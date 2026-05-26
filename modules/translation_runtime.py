from __future__ import annotations

from collections.abc import Callable, MutableMapping, Sequence
from dataclasses import dataclass

from modules.translation_engines import TranslationEngine
from utils.metrics import metrics


CacheKey = tuple[str, bool, str]
UntranslatedCheck = Callable[[str, str], bool]


@dataclass
class FallbackState:
    active_idx: int = 0
    probe_counter: int = 0
    consecutive_primary_failures: int = 0


def active_engine(
    engines: Sequence[TranslationEngine],
    active_idx: int,
) -> TranslationEngine | None:
    if not engines:
        return None
    if active_idx < 0 or active_idx >= len(engines):
        return None
    return engines[active_idx]


def cache_lookup(
    cache: MutableMapping[CacheKey, str],
    text: str,
    incomplete: bool,
    prompt_ver: str,
) -> str | None:
    key = (text, incomplete, prompt_ver)
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
) -> None:
    key = (text, incomplete, prompt_ver)
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
) -> str | None:
    if not engines:
        metrics.increment("translation.fallback.no_engines")
        return None

    # ── Probe: if hard-switched to fallback, periodically try primary ─────────
    if state.active_idx > 0:
        state.probe_counter += 1
        if state.probe_counter >= probe_every:
            state.probe_counter = 0
            metrics.increment("translation.fallback.probe")
            probe = engines[0].translate(text, system_prompt, incomplete, history)
            if probe and not looks_untranslated(probe, text):
                metrics.increment("translation.fallback.primary_recovered")
                log.info("Primary engine %s recovered; switching back", engines[0].engine_name)
                state.active_idx = 0
                state.consecutive_primary_failures = 0
                return probe
            if probe:
                metrics.increment("translation.bad_output")
            log.debug(
                "Primary probe failed, staying on %s",
                engines[state.active_idx].engine_name,
            )

    # ── Try primary (current hard-active) engine ──────────────────────────────
    primary_idx = state.active_idx
    metrics.increment("translation.fallback.attempt")
    primary = engines[primary_idx]
    result = primary.translate(text, system_prompt, incomplete, history)

    if result and not looks_untranslated(result, text):
        state.consecutive_primary_failures = 0
        return result

    if result:
        metrics.increment("translation.bad_output")

    state.consecutive_primary_failures += 1
    hard_switch = state.consecutive_primary_failures >= failure_threshold

    # ── Try fallback engines ──────────────────────────────────────────────────
    for index in range(primary_idx + 1, len(engines)):
        metrics.increment("translation.fallback.attempt")
        fb_result = engines[index].translate(text, system_prompt, incomplete, history)
        if fb_result and not looks_untranslated(fb_result, text):
            if hard_switch:
                # N consecutive failures — commit to this engine until probe recovers primary
                metrics.increment("translation.fallback.success")
                log.warning(
                    "Engine %s failed %d consecutive times; switching to %s",
                    primary.engine_name,
                    state.consecutive_primary_failures,
                    engines[index].engine_name,
                )
                state.active_idx = index
                state.consecutive_primary_failures = 0
                state.probe_counter = 0
            else:
                # Soft fallback: use this engine for this sentence only, retry primary next call
                log.info(
                    "Engine %s failed (failure %d/%d); using %s for this sentence",
                    primary.engine_name,
                    state.consecutive_primary_failures,
                    failure_threshold - 1,
                    engines[index].engine_name,
                )
            return fb_result
        if fb_result:
            metrics.increment("translation.bad_output")

    log.error("All engines failed for: %.40s", text)
    return None
