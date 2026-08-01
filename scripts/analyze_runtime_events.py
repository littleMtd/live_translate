from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from statistics import fmean
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import cfg
from utils.text_heuristics import STT_TEMPLATE_CONDITIONAL_PHRASES, STT_TEMPLATE_HARD_PHRASES

DEFAULT_LOG_DIR = PROJECT_ROOT / "logs"
QUEUE_LATENCY_FIELDS = (
    "engine_latency_ms",
    "queue_wait_ms",
    "output_delay_ms",
    "predecessor_stall_ms",
)
API_DIAGNOSTIC_FIELDS = (
    "api_total_wall_ms",
    "api_final_attempt_ms",
    "api_first_attempt_ms",
    "api_retry_attempt_ms",
    "retry_sleep_ms",
    "api_attempt_timeout_ms",
    "api_attempt_index",
    "api_inflight_count_at_start",
    "source_text_char_count",
    "prompt_char_count",
    "request_body_char_count",
    "message_count",
    "context_item_count",
)
ANALYZER_OUTPUT_NOTES = [
    "predecessor_stall_ms includes up to _TRANSLATION_LOOP_POLL_SEC of translator poll-gap noise.",
    "duplicate-suppressed translations still include ordering delay; output_delay_ms is pipeline delay, not user-visible subtitle delay.",
    "translation workers share recent/context/cache/fallback state; worker-local diagnostics remain isolated per call.",
]

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def analyze_runtime_events(
    path: Path | None = None,
    top_n: int = 10,
    labels_path: Path | None = None,
    run_kind: str = "live",
    run_id: str | None = None,
) -> dict[str, Any]:
    event_path = path or latest_event_file(DEFAULT_LOG_DIR)
    if event_path is None or not event_path.exists():
        return {
            "event_path": str(event_path) if event_path else "",
            "available": False,
            "reason": "runtime event file does not exist",
        }

    all_events = list(_read_events(event_path))
    normalized_run_kind = str(run_kind or "live").strip().lower()
    if normalized_run_kind not in {"live", "test", "replay", "benchmark", "all"}:
        raise ValueError("run_kind must be live, test, replay, benchmark, or all")
    events = [
        event
        for event in all_events
        if normalized_run_kind == "all" or _effective_run_kind(event) == normalized_run_kind
    ]
    # Single-run acceptance boundary (audit §15.6-2): a daily file can hold
    # many runs, and pre-v3 records count as legacy-live, so day totals mix
    # sessions. --run-id pins the report to exactly one run; "latest" resolves
    # to the newest run present after the run-kind filter (run ids are
    # timestamp-prefixed, so lexicographic max is newest).
    resolved_run_id = str(run_id or "").strip()
    if resolved_run_id:
        if resolved_run_id.lower() == "latest":
            candidate_ids = {str(event.get("run_id") or "") for event in events}
            candidate_ids.discard("")
            resolved_run_id = max(candidate_ids) if candidate_ids else ""
        events = [
            event for event in events
            if str(event.get("run_id") or "") == resolved_run_id
        ]
    translation_events = [event for event in events if event.get("event_type") == "translation"]
    fallback_events = [event for event in events if event.get("event_type") == "translation_fallback"]
    stt_events = [event for event in events if event.get("event_type") == "stt"]
    audio_events = [event for event in events if event.get("event_type") == "audio"]
    sentence_hold_shadow_events = [
        event
        for event in events
        if event.get("event_type") == "sentence_hold_shadow"
    ]
    sentence_early_cut_events = [
        event
        for event in events
        if event.get("event_type") == "sentence_early_cut"
    ]
    activity_shadow_events = [
        event
        for event in events
        if event.get("event_type") == "activity_shadow"
    ]
    activity_publication_events = [
        event
        for event in events
        if event.get("event_type") == "activity_publication"
    ]
    labels = load_run_labels(labels_path)
    latencies = [
        latency
        for event in translation_events
        if (latency := _float_or_none(event.get("latency_ms"))) is not None
    ]
    quality_flags = Counter(
        flag
        for event in translation_events
        for flag in event.get("quality_flags", [])
    )
    quality_classifications = Counter(
        classification
        for event in translation_events
        for classification in event.get("quality_classifications", [])
    )

    return {
        "event_path": str(event_path),
        "available": True,
        "run_kind_filter": normalized_run_kind,
        "run_id_filter": resolved_run_id or None,
        "unfiltered_total_events": len(all_events),
        "by_run_kind": _count_by(
            [{"run_kind": _effective_run_kind(event)} for event in all_events],
            "run_kind",
        ),
        "total_events": len(events),
        "translation_events": len(translation_events),
        "translation_fallback_events": len(fallback_events),
        "stt_events": len(stt_events),
        "audio_events": len(audio_events),
        "sentence_hold_shadow_events": len(sentence_hold_shadow_events),
        "sentence_early_cut_events": len(sentence_early_cut_events),
        "activity_shadow_events": len(activity_shadow_events),
        "activity_publication_events": len(activity_publication_events),
        "run_ids": sorted({str(event.get("run_id", "")) for event in events if event.get("run_id")}),
        "by_status": _count_by(translation_events, "status"),
        "status_breakdown": _status_breakdown(translation_events),
        "by_result_source": _count_by(translation_events, "result_source"),
        "by_cache_status": _count_by(translation_events, "cache_status"),
        "by_engine": _count_by(translation_events, "engine"),
        "by_activity_source": _count_by(
            translation_events,
            "activity_source",
        ),
        "by_filter_reason": _count_by(
            [e for e in translation_events if e.get("filter_reason")],
            "filter_reason",
        ),
        "by_subtitle_emitted": _count_by(translation_events, "subtitle_emitted"),
        "quality_flags": [
            {"flag": flag, "count": count}
            for flag, count in quality_flags.most_common()
        ],
        "quality_classifications": [
            {"classification": classification, "count": count}
            for classification, count in quality_classifications.most_common()
        ],
        "audio_summary": _audio_summary(audio_events),
        "stt_summary": _stt_summary(stt_events),
        "sentence_hold_shadow": _sentence_hold_shadow_summary(
            sentence_hold_shadow_events,
            top_n,
        ),
        "sentence_early_cut": _sentence_early_cut_summary(
            sentence_early_cut_events,
            translation_events,
            top_n,
        ),
        "activity_shadow": _activity_shadow_summary(activity_shadow_events),
        "activity_publication": _activity_publication_summary(
            activity_publication_events,
            translation_events,
        ),
        "latency_ms": _latency_summary(latencies),
        "queue_latency_ms": _queue_latency_summary(translation_events),
        "retry_summary": _retry_summary(translation_events),
        "source_fuzzy_shadow": _source_fuzzy_shadow_summary(
            translation_events,
            top_n,
        ),
        "api_diagnostics": _api_diagnostics_summary(translation_events),
        "dependency_markers": _dependency_marker_summary(translation_events),
        "translation_fallback": _fallback_summary(fallback_events),
        "analyzer_output_notes": ANALYZER_OUTPUT_NOTES,
        "runs": _run_summaries(
            translation_events,
            fallback_events,
            stt_events,
            audio_events,
            activity_shadow_events,
            activity_publication_events,
            labels,
            top_n,
        ),
        "latest": _latest_samples(translation_events, top_n),
        "flagged_samples": _flagged_samples(translation_events, top_n),
        "empty_targets": _empty_target_summary(translation_events, top_n),
    }


def latest_event_file(log_dir: Path = DEFAULT_LOG_DIR) -> Path | None:
    files = sorted(log_dir.glob("runtime_events_*.jsonl"), key=lambda path: path.stat().st_mtime)
    return files[-1] if files else None


def _effective_run_kind(event: Mapping[str, Any]) -> str:
    """Treat pre-v3 records as legacy live data; v3+ must identify its kind."""
    value = str(event.get("run_kind") or "").strip().lower()
    if value:
        return value
    return "live" if int(event.get("schema_version") or 0) < 3 else "unknown"


def _read_events(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _count_by(events: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    counts = Counter(str(event.get(key) or "unknown") for event in events)
    return [{"value": value, "count": count} for value, count in counts.most_common()]


def load_run_labels(path: Path | None = None) -> dict[str, dict[str, str]]:
    if path is None or not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, Mapping):
        return {}
    entries = raw.get("runs", raw)
    if not isinstance(entries, Mapping):
        return {}

    labels: dict[str, dict[str, str]] = {}
    for run_id, value in entries.items():
        if isinstance(value, str):
            labels[str(run_id)] = {"label": value, "note": ""}
        elif isinstance(value, Mapping):
            labels[str(run_id)] = {
                "label": str(value.get("label") or ""),
                "note": str(value.get("note") or ""),
            }
    return labels


def _run_summaries(
    events: list[dict[str, Any]],
    fallback_events: list[dict[str, Any]],
    stt_events: list[dict[str, Any]],
    audio_events: list[dict[str, Any]],
    activity_shadow_events: list[dict[str, Any]],
    activity_publication_events: list[dict[str, Any]],
    labels: dict[str, dict[str, str]],
    top_n: int,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        grouped.setdefault(str(event.get("run_id") or "unknown"), []).append(event)
    grouped_stt: dict[str, list[dict[str, Any]]] = {}
    for event in stt_events:
        grouped_stt.setdefault(str(event.get("run_id") or "unknown"), []).append(event)
    grouped_audio: dict[str, list[dict[str, Any]]] = {}
    for event in audio_events:
        grouped_audio.setdefault(str(event.get("run_id") or "unknown"), []).append(event)
    grouped_fallback: dict[str, list[dict[str, Any]]] = {}
    for event in fallback_events:
        grouped_fallback.setdefault(str(event.get("run_id") or "unknown"), []).append(event)
    grouped_activity_shadow: dict[str, list[dict[str, Any]]] = {}
    for event in activity_shadow_events:
        grouped_activity_shadow.setdefault(
            str(event.get("run_id") or "unknown"), []
        ).append(event)
    grouped_activity_publication: dict[str, list[dict[str, Any]]] = {}
    for event in activity_publication_events:
        grouped_activity_publication.setdefault(
            str(event.get("run_id") or "unknown"), []
        ).append(event)

    summaries = []
    for run_id, run_events in grouped.items():
        label = labels.get(run_id, {})
        template_events = [event for event in run_events if _has_template_phrase(event)]
        quality_flags = Counter(
            flag
            for event in run_events
            for flag in event.get("quality_flags", [])
        )
        quality_classifications = Counter(
            classification
            for event in run_events
            for classification in event.get("quality_classifications", [])
        )
        summaries.append(
            {
                "run_id": run_id,
                "run_kind": _effective_run_kind(run_events[0]),
                "git_sha": str(run_events[0].get("git_sha") or ""),
                "git_dirty": run_events[0].get("git_dirty"),
                "label": label.get("label", ""),
                "note": label.get("note", ""),
                **_time_summary(run_events),
                "translation_events": len(run_events),
                "status_breakdown": _status_breakdown(run_events),
                "by_status": _count_by(run_events, "status"),
                "by_result_source": _count_by(run_events, "result_source"),
                "by_cache_status": _count_by(run_events, "cache_status"),
                "by_engine": _count_by(run_events, "engine"),
                "by_activity_source": _count_by(
                    run_events,
                    "activity_source",
                ),
                "by_filter_reason": _count_by(
                    [event for event in run_events if event.get("filter_reason")],
                    "filter_reason",
                ),
                "by_subtitle_emitted": _count_by(run_events, "subtitle_emitted"),
                "quality_flags": [
                    {"flag": flag, "count": count}
                    for flag, count in quality_flags.most_common()
                ],
                "quality_classifications": [
                    {"classification": classification, "count": count}
                    for classification, count in quality_classifications.most_common()
                ],
                "latency_ms": _latency_summary(
                    [
                        latency
                        for event in run_events
                        if (latency := _float_or_none(event.get("latency_ms"))) is not None
                    ]
                ),
                "stt": _stt_summary(grouped_stt.get(run_id, [])),
                "audio": _audio_summary(grouped_audio.get(run_id, [])),
                "success_latency_ms": _latency_summary(
                    [
                        latency
                        for event in run_events
                        if event.get("status") == "success"
                        and (latency := _float_or_none(event.get("latency_ms"))) is not None
                    ]
                ),
                "queue_latency_ms": _queue_latency_summary(run_events),
                "retry_summary": _retry_summary(run_events),
                "source_fuzzy_shadow": _source_fuzzy_shadow_summary(
                    run_events,
                    top_n,
                ),
                "api_diagnostics": _api_diagnostics_summary(run_events),
                "dependency_markers": _dependency_marker_summary(run_events),
                "translation_fallback": _fallback_summary(grouped_fallback.get(run_id, [])),
                "activity_shadow": _activity_shadow_summary(
                    grouped_activity_shadow.get(run_id, [])
                ),
                "activity_publication": _activity_publication_summary(
                    grouped_activity_publication.get(run_id, []),
                    run_events,
                ),
                "template_hits": {
                    "total": len(template_events),
                    "by_status": _count_by(template_events, "status"),
                    "by_filter_reason": _count_by(
                        [event for event in template_events if event.get("filter_reason")],
                        "filter_reason",
                    ),
                },
                "template_success_samples": [
                    _sample(event)
                    for event in template_events
                    if event.get("status") == "success"
                ][:top_n],
                "flagged_samples": _flagged_samples(run_events, top_n),
            }
        )

    return sorted(summaries, key=lambda item: item.get("started_at") or "")


def _activity_shadow_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    request_ids = {
        str(event.get("capture_request_id") or "")
        for event in events
        if event.get("capture_request_id")
    }
    confirmed = [event for event in events if event.get("confirmed") is True]
    accepted = [event for event in events if event.get("shadow_accepted") is True]
    reused = [event for event in events if event.get("evidence_reused") is True]
    distinct = [event for event in events if event.get("distinct_frame") is True]
    publication_violations = []
    for event in events:
        mode = str(event.get("mode") or "")
        if event.get("stt_terms_applied") is True:
            publication_violations.append(event)
        elif mode == "record_only" and (
            event.get("published") is True
            or event.get("translation_context_applied") is True
        ):
            publication_violations.append(event)
        elif (
            event.get("candidate_open_set") is True
            and event.get("open_set_publication_enabled") is not True
            and (
                event.get("published") is True
                or event.get("translation_context_applied") is True
            )
        ):
            publication_violations.append(event)
        elif mode not in {"record_only", "translation_only"}:
            publication_violations.append(event)
    attempt_rows: list[dict[str, Any]] = []
    fallback_events = 0
    fallback_successes = 0
    for event in events:
        raw_chain = event.get("vision_attempt_chain")
        if isinstance(raw_chain, list) and raw_chain:
            chain = [
                {
                    key: attempt.get(key)
                    for key in (
                        "provider",
                        "model",
                        "outcome",
                        "retryable",
                        "error_type",
                        "http_status",
                        "latency_ms",
                        "prompt_tokens",
                        "completion_tokens",
                        "total_tokens",
                        "api_cost_usd",
                        "rate_limit_tpm",
                        "rate_limit_remaining_tokens",
                        "rate_limit_reset_tokens_sec",
                    )
                }
                for attempt in raw_chain
                if isinstance(attempt, dict)
            ]
            if chain:
                attempt_rows.extend(chain)
                if len(chain) > 1:
                    fallback_events += 1
                    if event.get("vision_outcome") == "success":
                        fallback_successes += 1
                continue
        legacy_map = {
            "provider": "vision_provider",
            "model": "vision_model",
            "outcome": "vision_outcome",
            "retryable": "vision_retryable",
            "error_type": "vision_error_type",
            "http_status": "vision_http_status",
            "latency_ms": "vision_latency_ms",
            "prompt_tokens": "vision_prompt_tokens",
            "completion_tokens": "vision_completion_tokens",
            "total_tokens": "vision_total_tokens",
            "api_cost_usd": "vision_api_cost_usd",
            "rate_limit_tpm": "vision_rate_limit_tpm",
            "rate_limit_remaining_tokens": (
                "vision_rate_limit_remaining_tokens"
            ),
            "rate_limit_reset_tokens_sec": (
                "vision_rate_limit_reset_tokens_sec"
            ),
        }
        legacy = {
            key: event.get(source)
            for key, source in legacy_map.items()
        }
        if any(value not in {None, ""} for value in legacy.values()):
            attempt_rows.append(legacy)
    usage_fields = (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
    )
    usage_events = [
        attempt
        for attempt in attempt_rows
        if any(
            _float_or_none(attempt.get(field)) is not None
            for field in usage_fields
        )
    ]
    rate_fields = (
        "rate_limit_tpm",
        "rate_limit_remaining_tokens",
        "rate_limit_reset_tokens_sec",
    )
    rate_events = [
        attempt
        for attempt in attempt_rows
        if any(
            _float_or_none(attempt.get(field)) is not None
            for field in rate_fields
        )
    ]
    remaining_tokens = [
        value
        for attempt in attempt_rows
        if (
            value := _float_or_none(
                attempt.get("rate_limit_remaining_tokens")
            )
        )
        is not None
    ]
    reset_seconds = [
        value
        for attempt in attempt_rows
        if (
            value := _float_or_none(
                attempt.get("rate_limit_reset_tokens_sec")
            )
        )
        is not None
    ]
    tpm_limits = [
        value
        for attempt in attempt_rows
        if (
            value := _float_or_none(attempt.get("rate_limit_tpm"))
        )
        is not None
    ]

    def count_present(key: str) -> list[dict[str, Any]]:
        return _count_by(
            [event for event in events if event.get(key) not in {None, ""}],
            key,
        )

    return {
        "total_events": len(events),
        "vision_request_count": len(request_ids),
        "confirmed_count": len(confirmed),
        "shadow_accepted_count": len(accepted),
        "duplicate_evidence_count": len(reused),
        "distinct_frame_count": len(distinct),
        "manual_override_event_count": sum(
            event.get("manual_override_active") is True for event in events
        ),
        "publication_violation_count": len(publication_violations),
        "by_vision_provider": count_present("vision_provider"),
        "by_vision_model": count_present("vision_model"),
        "by_vision_outcome": count_present("vision_outcome"),
        "by_vision_error_type": count_present("vision_error_type"),
        "by_vision_http_status": count_present("vision_http_status"),
        "by_window_status": _count_by(events, "window_status"),
        "by_capture_status": _count_by(events, "capture_status"),
        "by_discard_reason": _count_by(events, "discard_reason"),
        "by_candidate_activity_id": _count_by(
            [
                event
                for event in events
                if event.get("candidate_activity_id")
            ],
            "candidate_activity_id",
        ),
        "by_candidate_activity_kind": _count_by(
            [
                event
                for event in events
                if event.get("candidate_activity_kind")
            ],
            "candidate_activity_kind",
        ),
        "by_activity_parse_status": _count_by(
            [
                event
                for event in events
                if event.get("activity_parse_status")
            ],
            "activity_parse_status",
        ),
        "by_activity_rejection_reason": _count_by(
            [
                event
                for event in events
                if event.get("activity_rejection_reason")
            ],
            "activity_rejection_reason",
        ),
        "vision_latency_ms": _latency_summary(
            [
                latency
                for event in events
                if (
                    latency := _float_or_none(event.get("vision_latency_ms"))
                )
                is not None
            ]
        ),
        "vision_usage": {
            "event_count": len(usage_events),
            "prompt_tokens": int(
                sum(
                    _float_or_none(attempt.get("prompt_tokens")) or 0
                    for attempt in usage_events
                )
            ),
            "completion_tokens": int(
                sum(
                    _float_or_none(attempt.get("completion_tokens")) or 0
                    for attempt in usage_events
                )
            ),
            "total_tokens": int(
                sum(
                    _float_or_none(attempt.get("total_tokens")) or 0
                    for attempt in usage_events
                )
            ),
        },
        "vision_attempts": {
            "total": len(attempt_rows),
            "fallback_event_count": fallback_events,
            "fallback_success_count": fallback_successes,
            "by_provider": _count_by(attempt_rows, "provider"),
            "by_model": _count_by(attempt_rows, "model"),
            "by_outcome": _count_by(attempt_rows, "outcome"),
            "by_error_type": _count_by(
                [
                    attempt
                    for attempt in attempt_rows
                    if attempt.get("error_type")
                ],
                "error_type",
            ),
            "latency_ms": _latency_summary(
                [
                    latency
                    for attempt in attempt_rows
                    if (
                        latency := _float_or_none(
                            attempt.get("latency_ms")
                        )
                    )
                    is not None
                ]
            ),
            "api_cost_usd": round(
                sum(
                    _float_or_none(attempt.get("api_cost_usd")) or 0.0
                    for attempt in attempt_rows
                ),
                10,
            ),
        },
        "vision_rate_limit": {
            "observation_count": len(rate_events),
            **(
                {
                    "observed_tpm_limits": sorted(
                        {int(value) for value in tpm_limits}
                    )
                }
                if tpm_limits
                else {}
            ),
            **(
                {"minimum_remaining_tokens": int(min(remaining_tokens))}
                if remaining_tokens
                else {}
            ),
            **(
                {"maximum_reset_tokens_sec": round(max(reset_seconds), 3)}
                if reset_seconds
                else {}
            ),
        },
    }


def _activity_publication_summary(
    events: list[dict[str, Any]],
    translation_events: list[dict[str, Any]],
) -> dict[str, Any]:
    def is_open_set_event(event: dict[str, Any]) -> bool:
        return bool(
            event.get("open_set_activity") is True
            or str(event.get("activity_id") or "").startswith("auto-")
        )

    transition_violations = [
        event
        for event in events
        if event.get("stt_terms_applied") is True
        or event.get("translation_context_applied") is True
        or (
            str(event.get("mode") or "") == "record_only"
            and event.get("translation_context_available") is True
        )
        or (
            event.get("effective_source") == "automatic"
            and event.get("manual_override_active") is True
        )
        or (
            event.get("effective_source") == "automatic"
            and event.get("publication_enabled") is not True
        )
        or (
            event.get("effective_source") == "automatic"
            and is_open_set_event(event)
            and event.get("open_set_publication_enabled") is not True
        )
        or (
            event.get("action") == "published"
            and (
                event.get("effective_source") != "automatic"
                or event.get("automatic_available") is not True
                or event.get("translation_context_available") is not True
                or event.get("manual_override_active") is True
                or not event.get("activity_id")
            )
        )
        or (
            event.get("action") == "manual_override"
            and (
                event.get("effective_source") != "manual"
                or event.get("manual_override_active") is not True
                or event.get("translation_context_available") is True
                or not event.get("activity_id")
            )
        )
        or (
            event.get("action") == "cleared"
            and (
                event.get("effective_source") != "none"
                or event.get("automatic_available") is True
                or event.get("manual_override_active") is True
                or event.get("translation_context_available") is True
            )
        )
    ]
    automatic_translation_events = [
        event
        for event in translation_events
        if event.get("activity_source") == "automatic"
    ]
    valid_publication_run_ids = {
        str(event.get("run_id") or "unknown")
        for event in events
        if event.get("mode") == "translation_only"
        and event.get("publication_enabled") is True
        and event.get("action") == "published"
        and event.get("effective_source") == "automatic"
        and event.get("automatic_available") is True
        and event.get("translation_context_available") is True
        and event.get("manual_override_active") is not True
        and (
            not is_open_set_event(event)
            or event.get("open_set_publication_enabled") is True
        )
    }
    application_without_publication = [
        event
        for event in automatic_translation_events
        if str(event.get("run_id") or "unknown")
        not in valid_publication_run_ids
    ]
    return {
        "total_events": len(events),
        "translation_context_applied_count": len(
            automatic_translation_events
        ),
        "translation_context_available_count": sum(
            event.get("translation_context_available") is True
            for event in events
        ),
        "manual_override_event_count": sum(
            event.get("manual_override_active") is True
            for event in events
        ),
        "application_without_publication_count": len(
            application_without_publication
        ),
        "safety_violation_count": (
            len(transition_violations)
            + len(application_without_publication)
        ),
        "by_action": _count_by(events, "action"),
        "by_effective_source": _count_by(events, "effective_source"),
        "by_translation_activity_source": _count_by(
            translation_events,
            "activity_source",
        ),
        "by_reason": _count_by(events, "reason"),
        "by_activity_id": _count_by(
            [event for event in events if event.get("activity_id")],
            "activity_id",
        ),
        "by_activity_kind": _count_by(
            [event for event in events if event.get("activity_kind")],
            "activity_kind",
        ),
    }


def _time_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    timestamps = [
        parsed
        for event in events
        if (parsed := _parse_datetime(event.get("created_at"))) is not None
    ]
    if not timestamps:
        return {"started_at": "", "ended_at": "", "duration_sec": 0.0}
    started = min(timestamps)
    ended = max(timestamps)
    return {
        "started_at": started.isoformat(),
        "ended_at": ended.isoformat(),
        "duration_sec": round((ended - started).total_seconds(), 3),
    }


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _has_template_phrase(event: dict[str, Any]) -> bool:
    text = str(event.get("source_text") or "")
    return any(phrase in text for phrase in _TEMPLATE_PHRASES)


_TEMPLATE_PHRASES = tuple(STT_TEMPLATE_HARD_PHRASES) + tuple(STT_TEMPLATE_CONDITIONAL_PHRASES)


def _latency_summary(latencies: list[float]) -> dict[str, float | int]:
    if not latencies:
        return {"count": 0}
    ordered = sorted(latencies)
    n = len(ordered)

    def percentile(p: float) -> float:
        return ordered[min(n - 1, int(n * p))]

    return {
        "count": n,
        "avg": round(fmean(ordered), 2),
        "max": round(max(ordered), 2),
        "p50": round(percentile(0.50), 2),
        "p95": round(percentile(0.95), 2),
        "p99": round(percentile(0.99), 2),
    }


def _queue_latency_summary(events: list[dict[str, Any]]) -> dict[str, dict[str, float | int]]:
    return {
        field: _latency_summary(
            [
                value
                for event in events
                if (value := _float_or_none(event.get(field))) is not None
            ]
        )
        for field in QUEUE_LATENCY_FIELDS
    }


def _source_fuzzy_shadow_summary(
    events: list[dict[str, Any]],
    top_n: int,
) -> dict[str, Any]:
    rows: list[tuple[dict[str, Any], dict[str, Any]]] = [
        (event, shadow)
        for event in events
        if isinstance((shadow := event.get("source_fuzzy_shadow")), dict)
    ]
    candidate_rows = [
        (event, shadow)
        for event, shadow in rows
        if int(shadow.get("candidate_count") or 0) > 0
    ]
    candidates = [
        candidate
        for _event, shadow in rows
        for candidate in shadow.get("candidates", [])
        if isinstance(candidate, dict)
    ]
    unique = [
        candidate
        for candidate in candidates
        if candidate.get("decision") == "unique_match"
    ]
    ambiguous = [
        candidate
        for candidate in candidates
        if candidate.get("decision") == "ambiguous"
    ]
    pair_counts = Counter(
        (
            str(candidate.get("observed") or ""),
            str(candidate.get("canonical") or candidate.get("best_candidate") or ""),
            str(candidate.get("decision") or "unknown"),
        )
        for candidate in candidates
    )
    return {
        "observed_events": len(rows),
        "coverage_rate": round(len(rows) / len(events), 4) if events else 0.0,
        "enabled_events": sum(bool(shadow.get("enabled")) for _event, shadow in rows),
        "eligible_events": sum(bool(shadow.get("eligible")) for _event, shadow in rows),
        "candidate_events": len(candidate_rows),
        "candidate_event_rate": round(len(candidate_rows) / len(rows), 4) if rows else 0.0,
        "candidate_count": len(candidates),
        "unique_match_count": len(unique),
        "ambiguous_count": len(ambiguous),
        "would_change_events": sum(bool(shadow.get("would_change")) for _event, shadow in rows),
        "applied_count": sum(bool(shadow.get("applied")) for _event, shadow in rows),
        "diagnostic_errors": sum(
            shadow.get("reason") == "diagnostic_error"
            for _event, shadow in rows
        ),
        "by_profile": _count_by(
            [{"profile_id": shadow.get("profile_id")} for _event, shadow in rows],
            "profile_id",
        ),
        "by_reason": _count_by(
            [{"reason": shadow.get("reason")} for _event, shadow in rows],
            "reason",
        ),
        "pairs": [
            {
                "observed": observed,
                "candidate": candidate,
                "decision": decision,
                "count": count,
            }
            for (observed, candidate, decision), count in pair_counts.most_common(top_n)
        ],
        "samples": [
            {
                "run_id": event.get("run_id"),
                "sequence_id": event.get("sequence_id"),
                "profile_id": shadow.get("profile_id"),
                "source_text": _short(str(event.get("source_text") or "")),
                "proposed_text": _short(str(shadow.get("proposed_text") or "")),
                "candidates": shadow.get("candidates", []),
            }
            for event, shadow in candidate_rows[:top_n]
        ],
    }


def _retry_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    retry_events = [
        event
        for event in events
        if (_float_or_none(event.get("retry_count")) or 0) > 0
    ]
    retry_counts = [
        int(_float_or_none(event.get("retry_count")) or 0)
        for event in events
    ]
    quality_retry_events = [
        event
        for event in events
        if isinstance(event.get("quality_retry"), dict)
        and event.get("quality_retry")
    ]
    quality_retry_rows = [
        event["quality_retry"]
        for event in quality_retry_events
    ]
    return {
        "total_events": len(events),
        "retry_events": len(retry_events),
        "retry_rate": round(len(retry_events) / len(events), 3) if events else 0.0,
        "max_retry_count": max(retry_counts) if retry_counts else 0,
        "by_retry_reason": _count_by(
            [event for event in events if event.get("retry_reason")],
            "retry_reason",
        ),
        "quality_retry": {
            "events": len(quality_retry_events),
            "rate": (
                round(len(quality_retry_events) / len(events), 3)
                if events
                else 0.0
            ),
            "applied": sum(
                bool(row.get("applied"))
                for row in quality_retry_rows
            ),
            "by_trigger": _count_by(quality_retry_rows, "trigger"),
            "by_reason": _count_by(quality_retry_rows, "reason"),
        },
    }


def _api_diagnostics_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    attempt_chains = [
        event.get("attempts")
        for event in events
        if isinstance(event.get("attempts"), list) and event.get("attempts")
    ]
    attempts = [
        attempt
        for chain in attempt_chains
        for attempt in chain
        if isinstance(attempt, dict)
    ]
    hidden_timeout_events = sum(
        1
        for event in events
        if not (_float_or_none(event.get("api_timeout_count")) or 0)
        and any(
            (_float_or_none(attempt.get("api_timeout_count")) or 0) > 0
            for attempt in (event.get("attempts") or [])
            if isinstance(attempt, dict)
        )
    )
    api_events = [
        event
        for event in events
        if (_float_or_none(event.get("api_attempt_count")) or 0) > 0
    ]
    timeout_events = [
        event
        for event in api_events
        if (_float_or_none(event.get("api_timeout_count")) or 0) > 0
    ]
    retry_events = [
        event
        for event in api_events
        if (_float_or_none(event.get("api_attempt_count")) or 0) > 1
        or (_float_or_none(event.get("retry_sleep_ms")) or 0) > 0
    ]
    long_api_events = [
        event
        for event in api_events
        if (_float_or_none(event.get("api_total_wall_ms")) or 0) >= 10_000
    ]
    cost_rows: list[tuple[str, float]] = []
    for event in events:
        chain = [
            attempt
            for attempt in (event.get("attempts") or [])
            if isinstance(attempt, dict)
        ]
        sources = chain if chain else [event]
        for source in sources:
            cost = _float_or_none(source.get("api_cost_usd"))
            if cost is not None:
                cost_rows.append((str(source.get("engine") or ""), cost))
    cost_by_engine: dict[str, float] = {}
    for engine, cost in cost_rows:
        cost_by_engine[engine] = cost_by_engine.get(engine, 0.0) + cost
    return {
        "total_events": len(events),
        "api_events": len(api_events),
        "timeout_events": len(timeout_events),
        "timeout_rate": round(len(timeout_events) / len(api_events), 3) if api_events else 0.0,
        "retry_events": len(retry_events),
        "retry_rate": round(len(retry_events) / len(api_events), 3) if api_events else 0.0,
        "long_api_ge_10s": len(long_api_events),
        "long_api_ge_10s_timeout_events": len(
            [
                event
                for event in long_api_events
                if (_float_or_none(event.get("api_timeout_count")) or 0) > 0
            ]
        ),
        "by_attempt_count": _count_by(api_events, "api_attempt_count"),
        "by_timeout_count": _count_by(api_events, "api_timeout_count"),
        "by_error_type": _count_by(
            [event for event in api_events if event.get("api_error_type")],
            "api_error_type",
        ),
        "by_error_message_class": _count_by(
            [event for event in api_events if event.get("api_error_message_class")],
            "api_error_message_class",
        ),
        "cost_usd": {
            "observations": len(cost_rows),
            "total": round(sum(cost for _, cost in cost_rows), 8),
            "by_engine": [
                {"engine": engine, "cost_usd": round(cost, 8)}
                for engine, cost in sorted(cost_by_engine.items())
            ],
        },
        "attempt_chain": {
            "events_with_chain": len(attempt_chains),
            "total_attempts": len(attempts),
            "fallback_chain_attempts": sum(
                attempt.get("phase") == "fallback_chain" for attempt in attempts
            ),
            "quality_retry_attempts": sum(
                attempt.get("phase") == "quality_retry" for attempt in attempts
            ),
            "selected_attempts": sum(
                bool(attempt.get("selected_for_output")) for attempt in attempts
            ),
            "hidden_timeout_events": hidden_timeout_events,
            "by_engine": _count_by(attempts, "engine"),
            "by_status": _count_by(attempts, "status"),
        },
        "fields": {
            field: _latency_summary(
                [
                    value
                    for event in api_events
                    if (value := _float_or_none(event.get(field))) is not None
                ]
            )
            for field in API_DIAGNOSTIC_FIELDS
        },
    }


def _dependency_marker_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    marker_events = [event for event in events if bool(event.get("starts_with_dependency_marker"))]
    return {
        "total_events": len(events),
        "marker_events": len(marker_events),
        "marker_ratio": round(len(marker_events) / len(events), 3) if events else 0.0,
        "by_marker": _count_by(
            [event for event in marker_events if event.get("dependency_marker")],
            "dependency_marker",
        ),
    }


def _status_breakdown(events: list[dict[str, Any]]) -> dict[str, int]:
    """Separate denominators for success / filtered / failed / other.

    `by_status` shows the distribution as a list; this returns a flat dict so
    callers (digest, dashboards) can compute ratios directly without parsing.
    """
    counts = {"total": len(events), "success": 0, "filtered": 0, "failed": 0, "other": 0}
    for event in events:
        status = event.get("status") or "other"
        if status in counts and status != "total":
            counts[status] += 1
        else:
            counts["other"] += 1
    return counts


def _fallback_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    probe_events = [
        event
        for event in events
        if event.get("action") in {"probe_succeeded", "probe_failed"}
    ]
    successful_probes = [
        event for event in probe_events if event.get("action") == "probe_succeeded"
    ]
    failed_probes = [
        event for event in probe_events if event.get("action") == "probe_failed"
    ]
    circuit_open_events = [
        event for event in events if event.get("action") == "circuit_opened"
    ]
    return {
        "total": len(events),
        "by_action": _count_by(events, "action"),
        "by_probe_status": _count_by(
            [event for event in events if event.get("probe_status")],
            "probe_status",
        ),
        "circuits_opened": sum(
            event.get("action") == "circuit_opened" for event in events
        ),
        "circuit_open_by_failure_scope": _count_by(
            circuit_open_events,
            "failure_scope",
        ),
        "circuit_open_by_error_type": _count_by(
            circuit_open_events,
            "api_error_type",
        ),
        "circuit_open_by_error_message_class": _count_by(
            circuit_open_events,
            "api_error_message_class",
        ),
        "fallback_advances": sum(
            event.get("action") == "fallback_advanced" for event in events
        ),
        "circuits_closed": sum(
            event.get("action") == "circuit_closed" for event in events
        ),
        "probe_attempts": len(probe_events),
        "successful_probes": len(successful_probes),
        "failed_probes": len(failed_probes),
        "cooldown_skips": sum(
            event.get("action") == "probe_cooldown_skipped" for event in events
        ),
        "max_probe_success_streak": max(
            (
                int(_float_or_none(event.get("probe_success_streak")) or 0)
                for event in successful_probes
            ),
            default=0,
        ),
        "probe_latency_ms": _latency_summary(
            [
                latency
                for event in probe_events
                if (latency := _float_or_none(event.get("probe_elapsed_ms"))) is not None
            ]
        ),
        "probe_history_items": _latency_summary(
            [
                count
                for event in probe_events
                if (count := _float_or_none(event.get("probe_history_items"))) is not None
            ]
        ),
    }


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _latest_samples(events: list[dict[str, Any]], top_n: int) -> list[dict[str, Any]]:
    return [_sample(event) for event in events[-top_n:]]


def _flagged_samples(events: list[dict[str, Any]], top_n: int) -> list[dict[str, Any]]:
    return [_sample(event) for event in events if event.get("quality_flags")][:top_n]


def _empty_target_summary(events: list[dict[str, Any]], top_n: int) -> dict[str, Any]:
    empty_events = [
        event
        for event in events
        if str(event.get("target_text") or "").strip() == ""
    ]
    return {
        "total": len(empty_events),
        "by_status": _count_by(empty_events, "status"),
        "by_result_source": _count_by(empty_events, "result_source"),
        "by_filter_reason": _count_by(
            [event for event in empty_events if event.get("filter_reason")],
            "filter_reason",
        ),
        "samples": [_sample(event) for event in empty_events[:top_n]],
    }


def _stt_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    request_events = [event for event in events if bool(event.get("request_sent"))]
    requests_sent = len(request_events)
    return {
        "total": len(events),
        "requests_sent": requests_sent,
        "request_budget": _request_budget(requests_sent),
        "audio_seconds_total": round(
            sum(_float_or_none(event.get("audio_seconds")) or 0 for event in events),
            3,
        ),
        "audio_seconds_sent": round(
            sum(_float_or_none(event.get("audio_seconds")) or 0 for event in request_events),
            3,
        ),
        "by_status": _count_by(events, "status"),
        "by_reason": _count_by([event for event in events if event.get("reason")], "reason"),
        "by_engine": _count_by(events, "engine"),
        "latency_ms": _latency_summary(
            [
                latency
                for event in events
                if (latency := _float_or_none(event.get("latency_ms"))) is not None
            ]
        ),
    }


def _sentence_early_cut_summary(
    events: list[dict[str, Any]],
    translation_events: list[dict[str, Any]],
    top_n: int,
) -> dict[str, Any]:
    def decision_key(event: dict[str, Any]) -> tuple[str, str]:
        return (
            str(event.get("run_id") or ""),
            str(event.get("decision_id") or ""),
        )

    keyed: dict[tuple[str, str], list[dict[str, Any]]] = {}
    missing_key_count = 0
    for event in events:
        key = decision_key(event)
        if not all(key):
            missing_key_count += 1
            continue
        keyed.setdefault(key, []).append(event)
    canonical = [group[0] for group in keyed.values()]
    candidates = [event for event in canonical if event.get("would_cut") is True]
    legacy_overlap = [
        event for event in candidates if event.get("legacy_would_cut") is True
    ]
    actionable = [
        event for event in candidates if event.get("legacy_would_cut") is not True
    ]
    applied = [event for event in canonical if event.get("applied") is True]
    def provider_request_count(event: dict[str, Any]) -> int:
        attempts = event.get("attempts")
        if isinstance(attempts, list):
            nested_count = sum(
                max(
                    0,
                    int(_float_or_none(attempt.get("api_attempt_count")) or 0),
                )
                for attempt in attempts
                if isinstance(attempt, dict)
            )
            if nested_count:
                return nested_count
        return max(
            int(_float_or_none(event.get("api_attempt_count")) or 0),
            1 if event.get("result_source") == "api" else 0,
        )

    translation_request_count = sum(
        provider_request_count(event) for event in translation_events
    )

    batch_sizes: dict[tuple[str, str], float] = {}
    for event in canonical:
        batch_id = str(event.get("drain_batch_id") or "")
        batch_size = _float_or_none(event.get("drain_batch_size"))
        if batch_id and batch_size is not None:
            batch_sizes[(str(event.get("run_id") or ""), batch_id)] = batch_size

    saved_wait = [
        value
        for event in actionable
        if (value := _float_or_none(event.get("saved_wait_ms"))) is not None
    ]
    max_text_drops = max(
        (
            int(_float_or_none(event.get("text_queue_drops")) or 0)
            for event in canonical
        ),
        default=0,
    )
    max_sentence_drops = max(
        (
            int(_float_or_none(event.get("sentence_queue_drops")) or 0)
            for event in canonical
        ),
        default=0,
    )

    samples = [
        {
            "run_id": event.get("run_id"),
            "decision_id": event.get("decision_id"),
            "mode": event.get("mode"),
            "classification": event.get("classification"),
            "reason_code": event.get("reason_code"),
            "candidate_kind": event.get("candidate_kind"),
            "candidate_text": _short(str(event.get("candidate_text") or "")),
            "residual_text": _short(str(event.get("residual_text") or "")),
            "legacy_would_cut": event.get("legacy_would_cut"),
            "would_cut": event.get("would_cut"),
            "applied": event.get("applied"),
            "saved_wait_ms": event.get("saved_wait_ms"),
            "drain_batch_position": event.get("drain_batch_position"),
            "drain_batch_size": event.get("drain_batch_size"),
            "actual_cut_reason": event.get("actual_cut_reason"),
        }
        for event in candidates[:top_n]
    ]
    return {
        "event_count": len(events),
        "decision_count": len(canonical),
        "missing_decision_key_count": missing_key_count,
        "duplicate_decision_count": sum(
            max(0, len(group) - 1) for group in keyed.values()
        ),
        "by_mode": _count_by(canonical, "mode"),
        "by_classification": _count_by(canonical, "classification"),
        "by_reason_code": _count_by(canonical, "reason_code"),
        "by_candidate_kind": _count_by(candidates, "candidate_kind"),
        "would_cut_count": len(candidates),
        "legacy_overlap_count": len(legacy_overlap),
        "actionable_would_cut_count": len(actionable),
        "applied_count": len(applied),
        "candidate_rate": (
            round(len(candidates) / len(canonical), 4) if canonical else 0.0
        ),
        "actionable_rate": (
            round(len(actionable) / len(canonical), 4) if canonical else 0.0
        ),
        "projected_additional_request_upper_bound_ratio": (
            round(len(actionable) / translation_request_count, 4)
            if translation_request_count
            else None
        ),
        "translation_request_count": translation_request_count,
        "saved_wait_ms": _latency_summary(saved_wait),
        "drain_batch_size": _latency_summary(list(batch_sizes.values())),
        "multi_item_batch_count": sum(size > 1 for size in batch_sizes.values()),
        "max_text_queue_drops": max_text_drops,
        "max_sentence_queue_drops": max_sentence_drops,
        "by_actual_cut_reason": _count_by(
            [event for event in canonical if event.get("actual_cut_reason")],
            "actual_cut_reason",
        ),
        "samples": samples,
    }


def _sentence_hold_shadow_summary(
    events: list[dict[str, Any]],
    top_n: int,
) -> dict[str, Any]:
    candidate_events = [event for event in events if event.get("phase") == "candidate"]
    outcome_events = [event for event in events if event.get("phase") == "outcome"]

    def key(event: dict[str, Any]) -> tuple[str, str]:
        return (
            str(event.get("run_id") or ""),
            str(event.get("shadow_id") or ""),
        )

    candidates_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for event in candidate_events:
        if key(event)[1]:
            candidates_by_key.setdefault(key(event), []).append(event)
    outcomes_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for event in outcome_events:
        if key(event)[1]:
            outcomes_by_key.setdefault(key(event), []).append(event)

    # Keep one canonical event per key for rates. Duplicates and orphans are
    # exposed separately rather than silently multiplying opportunity counts.
    candidates = [group[0] for group in candidates_by_key.values()]
    matched_outcomes = {
        event_key: outcomes_by_key[event_key][0]
        for event_key in candidates_by_key
        if event_key in outcomes_by_key
    }
    outcomes = list(matched_outcomes.values())
    observed = [event for event in outcomes if event.get("observed_next_chunk") is True]
    raw_continuations = [
        event
        for event in observed
        if event.get("raw_continuation_heuristic") is True
    ]
    useful = [event for event in observed if event.get("useful_merge_heuristic") is True]
    useful_300 = [event for event in useful if event.get("within_300ms") is True]
    useful_500 = [event for event in useful if event.get("within_500ms") is True]
    candidate_count = len(candidates)

    def rate(count: int) -> float:
        return round(count / candidate_count, 4) if candidate_count else 0.0

    signal_counts = Counter(
        str(signal)
        for event in candidates
        for signal in event.get("signals", [])
    )
    matched_ending_counts = Counter(
        str(event.get("matched_ending") or "")
        for event in candidates
        if event.get("matched_ending")
    )
    useful_500_keys = {
        key(event)
        for event in useful_500
    }
    signal_useful_500 = Counter(
        str(signal)
        for event in candidates
        if key(event) in useful_500_keys
        for signal in event.get("signals", [])
    )

    def opportunity_for(disposition: str) -> dict[str, Any]:
        scoped_candidates = [
            event
            for event in candidates
            if str(event.get("disposition") or "") == disposition
        ]
        scoped_keys = {key(event) for event in scoped_candidates}
        scoped_outcomes = [
            outcome
            for event_key, outcome in matched_outcomes.items()
            if event_key in scoped_keys
        ]
        scoped_useful = [
            outcome
            for outcome in scoped_outcomes
            if outcome.get("observed_next_chunk") is True
            and outcome.get("useful_merge_heuristic") is True
        ]
        scoped_raw = [
            outcome
            for outcome in scoped_outcomes
            if outcome.get("observed_next_chunk") is True
            and outcome.get("raw_continuation_heuristic") is True
        ]
        scoped_300 = [
            outcome for outcome in scoped_useful if outcome.get("within_300ms") is True
        ]
        scoped_500 = [
            outcome for outcome in scoped_useful if outcome.get("within_500ms") is True
        ]
        denominator = len(scoped_candidates)

        def scoped_rate(count: int) -> float:
            return round(count / denominator, 4) if denominator else 0.0

        return {
            "candidate_count": denominator,
            "matched_outcome_count": len(scoped_outcomes),
            "raw_continuation_count": len(scoped_raw),
            "raw_continuation_rate": scoped_rate(len(scoped_raw)),
            "one_chunk_useful_count": len(scoped_useful),
            "one_chunk_useful_rate": scoped_rate(len(scoped_useful)),
            "useful_within_300ms_count": len(scoped_300),
            "useful_within_300ms_rate": scoped_rate(len(scoped_300)),
            "useful_within_500ms_count": len(scoped_500),
            "useful_within_500ms_rate": scoped_rate(len(scoped_500)),
        }

    samples = []
    for candidate in candidates[:top_n]:
        event_key = key(candidate)
        shadow_id = event_key[1]
        outcome = matched_outcomes.get(event_key, {})
        samples.append(
            {
                "run_id": event_key[0],
                "shadow_id": shadow_id,
                "signals": candidate.get("signals", []),
                "disposition": candidate.get("disposition"),
                "cut_reason": candidate.get("cut_reason"),
                "candidate_text": _short(str(candidate.get("candidate_text") or "")),
                "observed_next_chunk": outcome.get("observed_next_chunk"),
                "next_chunk_delay_ms": outcome.get("next_chunk_delay_ms"),
                "within_300ms": outcome.get("within_300ms"),
                "within_500ms": outcome.get("within_500ms"),
                "raw_continuation_heuristic": outcome.get(
                    "raw_continuation_heuristic"
                ),
                "useful_merge_heuristic": outcome.get("useful_merge_heuristic"),
                "next_chunk_text": _short(str(outcome.get("next_chunk_text") or "")),
            }
        )

    return {
        "candidate_count": candidate_count,
        "candidate_event_count": len(candidate_events),
        "outcome_event_count": len(outcome_events),
        "outcome_count": len(outcomes),
        "matched_outcome_count": len(outcomes),
        "unresolved_count": sum(
            event_key not in outcomes_by_key for event_key in candidates_by_key
        ),
        "orphan_outcome_count": sum(
            len(group)
            for event_key, group in outcomes_by_key.items()
            if event_key not in candidates_by_key
        ),
        "duplicate_candidate_count": sum(
            max(0, len(group) - 1) for group in candidates_by_key.values()
        ),
        "duplicate_outcome_count": sum(
            max(0, len(group) - 1)
            for event_key, group in outcomes_by_key.items()
            if event_key in candidates_by_key
        ),
        "observed_next_chunk_count": len(observed),
        "raw_continuation_count": len(raw_continuations),
        "raw_continuation_rate": rate(len(raw_continuations)),
        "one_chunk_useful_count": len(useful),
        "one_chunk_useful_rate": rate(len(useful)),
        "useful_within_300ms_count": len(useful_300),
        "useful_within_300ms_rate": rate(len(useful_300)),
        "useful_within_500ms_count": len(useful_500),
        "useful_within_500ms_rate": rate(len(useful_500)),
        "next_chunk_delay_ms": _latency_summary(
            [
                delay
                for event in observed
                if (delay := _float_or_none(event.get("next_chunk_delay_ms"))) is not None
            ]
        ),
        "by_signal": [
            {"signal": signal, "count": count}
            for signal, count in signal_counts.most_common()
        ],
        "by_matched_ending": [
            {"ending": ending, "count": count}
            for ending, count in matched_ending_counts.most_common()
        ],
        "useful_within_500ms_by_signal": [
            {"signal": signal, "count": signal_useful_500[signal]}
            for signal, _count in signal_counts.most_common()
        ],
        "by_disposition": _count_by(candidates, "disposition"),
        "actionable_emitted": opportunity_for("emitted"),
        "already_buffered": opportunity_for("buffered"),
        "by_cut_reason": _count_by(candidates, "cut_reason"),
        "by_outcome_reason": _count_by(outcomes, "outcome_reason"),
        "samples": samples,
    }


def _audio_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "total": len(events),
        "by_cut_reason": _count_by(events, "cut_reason"),
        "by_adaptive_active": _count_by(events, "adaptive_active"),
        "audio_seconds": _latency_summary(
            [
                seconds
                for event in events
                if (seconds := _float_or_none(event.get("audio_seconds"))) is not None
            ]
        ),
        "raw_audio_seconds": _latency_summary(
            [
                seconds
                for event in events
                if (seconds := _float_or_none(event.get("raw_audio_seconds"))) is not None
            ]
        ),
        "overlap_seconds": _latency_summary(
            [
                seconds
                for event in events
                if (seconds := _float_or_none(event.get("overlap_seconds"))) is not None
            ]
        ),
    }


def _request_budget(requests_sent: int) -> dict[str, float | int]:
    limit = max(0, int(getattr(cfg.stt, "groq_daily_request_limit", 0) or 0))
    remaining = max(0, limit - requests_sent) if limit else 0
    used_ratio = round(requests_sent / limit, 3) if limit else 0.0
    return {
        "limit": limit,
        "used": requests_sent,
        "remaining": remaining,
        "used_ratio": used_ratio,
    }


def _sample(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "created_at": event.get("created_at"),
        "status": event.get("status"),
        "result_source": event.get("result_source"),
        "cache_status": event.get("cache_status"),
        "engine": event.get("engine"),
        "filter_reason": event.get("filter_reason"),
        "latency_ms": event.get("latency_ms"),
        "quality_flags": event.get("quality_flags", []),
        "source_text": _short(event.get("source_text") or ""),
        "target_text": _short(event.get("target_text") or ""),
    }


def _short(text: str, limit: int = 90) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _print_report(report: dict[str, Any]) -> None:
    if not report.get("available"):
        print(f"Runtime events unavailable: {report['reason']} ({report['event_path']})")
        return

    print(f"Runtime events: {report['event_path']}")
    print(
        f"Events: {report['total_events']} | "
        f"Translations: {report['translation_events']} | "
        f"Fallback: {report.get('translation_fallback_events', 0)} | "
        f"STT: {report['stt_events']} | "
        f"Audio: {report.get('audio_events', 0)}"
    )
    print(f"Run IDs: {', '.join(report['run_ids'])}")
    print(
        f"Run-kind filter: {report['run_kind_filter']} | "
        f"run-id filter: {report.get('run_id_filter') or '(all runs)'} | "
        f"unfiltered={report['unfiltered_total_events']} | "
        f"by_kind={report['by_run_kind']}"
    )
    print(f"Status breakdown: {report['status_breakdown']}")
    print(f"Latency ms: {report['latency_ms']}")
    print(f"Queue latency ms: {report['queue_latency_ms']}")
    print(f"Retry summary: {report['retry_summary']}")
    print(f"Source fuzzy shadow: {report['source_fuzzy_shadow']}")
    print(f"API diagnostics: {report['api_diagnostics']}")
    print(f"Dependency markers: {report['dependency_markers']}")
    print(f"Translation fallback: {report['translation_fallback']}")
    print(f"Sentence hold shadow: {report['sentence_hold_shadow']}")
    print(f"Sentence early cut: {report['sentence_early_cut']}")
    print(f"Empty targets: {report['empty_targets']['total']}")
    print(
        "STT summary: "
        f"requests={report['stt_summary']['requests_sent']} | "
        f"budget={report['stt_summary']['request_budget']} | "
        f"audio_sent={report['stt_summary']['audio_seconds_sent']}s | "
        f"status={report['stt_summary']['by_status']} | "
        f"reasons={report['stt_summary']['by_reason']}"
    )
    print(
        "Audio summary: "
        f"chunks={report['audio_summary']['total']} | "
        f"cuts={report['audio_summary']['by_cut_reason']} | "
        f"adaptive={report['audio_summary']['by_adaptive_active']} | "
        f"seconds={report['audio_summary']['audio_seconds']}"
    )
    if report.get("runs"):
        print("\nRuns:")
        for run in report["runs"]:
            label = f" [{run['label']}]" if run.get("label") else ""
            print(
                f"- {run['run_id']}{label}: "
                f"{run['translation_events']} events, "
                f"{run['duration_sec']}s, "
                f"status={run['status_breakdown']}, "
                f"stt={run['stt']['by_status']}, "
                f"audio_cuts={run['audio']['by_cut_reason']}, "
                f"success_latency={run['success_latency_ms']}, "
                f"queue_latency={run['queue_latency_ms']}, "
                f"retry_rate={run['retry_summary']['retry_rate']}, "
                f"api_timeout_rate={run['api_diagnostics']['timeout_rate']}, "
                f"fallback={run['translation_fallback']}, "
                f"dependency_marker_ratio={run['dependency_markers']['marker_ratio']}, "
                f"template_hits={run['template_hits']['total']}"
            )
    if report.get("analyzer_output_notes"):
        print("\nAnalyzer output notes:")
        for note in report["analyzer_output_notes"]:
            print(f"- {note}")
    for title, key in (
        ("By status", "by_status"),
        ("By result source", "by_result_source"),
        ("By cache status", "by_cache_status"),
        ("By filter reason", "by_filter_reason"),
        ("By subtitle emitted", "by_subtitle_emitted"),
        ("Quality flags", "quality_flags"),
    ):
        print(f"\n{title}:")
        for item in report[key]:
            value = item.get("value") or item.get("flag")
            print(f"- {value}: {item['count']}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze runtime translation event JSONL logs.")
    parser.add_argument("--events", type=Path, default=None, help="Path to runtime_events_YYYYMMDD.jsonl.")
    parser.add_argument("--labels", type=Path, default=None, help="Optional JSON map of run_id to labels/notes.")
    parser.add_argument("--top", type=int, default=10, help="Number of sample rows per report section.")
    parser.add_argument(
        "--run-id",
        default=None,
        help="Analyze only this run_id; 'latest' resolves to the newest run after the run-kind filter.",
    )
    parser.add_argument(
        "--run-kind",
        choices=("live", "test", "replay", "benchmark", "all"),
        default="live",
        help="Analyze only this run kind (pre-v3 records are treated as legacy live).",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    report = analyze_runtime_events(args.events, args.top, args.labels, args.run_kind, args.run_id)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
