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
ANALYZER_OUTPUT_NOTES = [
    "predecessor_stall_ms includes up to _TRANSLATION_LOOP_POLL_SEC of translator poll-gap noise.",
    "duplicate-suppressed translations still include ordering delay; output_delay_ms is pipeline delay, not user-visible subtitle delay.",
    "per-worker Translator instances keep separate recent/context/cache state; cache_status and engine_latency comparisons have that known confound.",
]

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def analyze_runtime_events(
    path: Path | None = None,
    top_n: int = 10,
    labels_path: Path | None = None,
) -> dict[str, Any]:
    event_path = path or latest_event_file(DEFAULT_LOG_DIR)
    if event_path is None or not event_path.exists():
        return {
            "event_path": str(event_path) if event_path else "",
            "available": False,
            "reason": "runtime event file does not exist",
        }

    events = list(_read_events(event_path))
    translation_events = [event for event in events if event.get("event_type") == "translation"]
    stt_events = [event for event in events if event.get("event_type") == "stt"]
    audio_events = [event for event in events if event.get("event_type") == "audio"]
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

    return {
        "event_path": str(event_path),
        "available": True,
        "total_events": len(events),
        "translation_events": len(translation_events),
        "stt_events": len(stt_events),
        "audio_events": len(audio_events),
        "run_ids": sorted({str(event.get("run_id", "")) for event in events if event.get("run_id")}),
        "by_status": _count_by(translation_events, "status"),
        "status_breakdown": _status_breakdown(translation_events),
        "by_result_source": _count_by(translation_events, "result_source"),
        "by_cache_status": _count_by(translation_events, "cache_status"),
        "by_engine": _count_by(translation_events, "engine"),
        "by_filter_reason": _count_by(
            [e for e in translation_events if e.get("filter_reason")],
            "filter_reason",
        ),
        "by_subtitle_emitted": _count_by(translation_events, "subtitle_emitted"),
        "quality_flags": [
            {"flag": flag, "count": count}
            for flag, count in quality_flags.most_common()
        ],
        "audio_summary": _audio_summary(audio_events),
        "stt_summary": _stt_summary(stt_events),
        "latency_ms": _latency_summary(latencies),
        "queue_latency_ms": _queue_latency_summary(translation_events),
        "retry_summary": _retry_summary(translation_events),
        "dependency_markers": _dependency_marker_summary(translation_events),
        "analyzer_output_notes": ANALYZER_OUTPUT_NOTES,
        "runs": _run_summaries(translation_events, stt_events, audio_events, labels, top_n),
        "latest": _latest_samples(translation_events, top_n),
        "flagged_samples": _flagged_samples(translation_events, top_n),
        "empty_targets": _empty_target_summary(translation_events, top_n),
    }


def latest_event_file(log_dir: Path = DEFAULT_LOG_DIR) -> Path | None:
    files = sorted(log_dir.glob("runtime_events_*.jsonl"), key=lambda path: path.stat().st_mtime)
    return files[-1] if files else None


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
    stt_events: list[dict[str, Any]],
    audio_events: list[dict[str, Any]],
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

    summaries = []
    for run_id, run_events in grouped.items():
        label = labels.get(run_id, {})
        template_events = [event for event in run_events if _has_template_phrase(event)]
        quality_flags = Counter(
            flag
            for event in run_events
            for flag in event.get("quality_flags", [])
        )
        summaries.append(
            {
                "run_id": run_id,
                "label": label.get("label", ""),
                "note": label.get("note", ""),
                **_time_summary(run_events),
                "translation_events": len(run_events),
                "status_breakdown": _status_breakdown(run_events),
                "by_status": _count_by(run_events, "status"),
                "by_result_source": _count_by(run_events, "result_source"),
                "by_cache_status": _count_by(run_events, "cache_status"),
                "by_engine": _count_by(run_events, "engine"),
                "by_filter_reason": _count_by(
                    [event for event in run_events if event.get("filter_reason")],
                    "filter_reason",
                ),
                "by_subtitle_emitted": _count_by(run_events, "subtitle_emitted"),
                "quality_flags": [
                    {"flag": flag, "count": count}
                    for flag, count in quality_flags.most_common()
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
                "dependency_markers": _dependency_marker_summary(run_events),
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
    return {
        "total_events": len(events),
        "retry_events": len(retry_events),
        "retry_rate": round(len(retry_events) / len(events), 3) if events else 0.0,
        "max_retry_count": max(retry_counts) if retry_counts else 0,
        "by_retry_reason": _count_by(
            [event for event in events if event.get("retry_reason")],
            "retry_reason",
        ),
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
        f"STT: {report['stt_events']} | "
        f"Audio: {report.get('audio_events', 0)}"
    )
    print(f"Run IDs: {', '.join(report['run_ids'])}")
    print(f"Status breakdown: {report['status_breakdown']}")
    print(f"Latency ms: {report['latency_ms']}")
    print(f"Queue latency ms: {report['queue_latency_ms']}")
    print(f"Retry summary: {report['retry_summary']}")
    print(f"Dependency markers: {report['dependency_markers']}")
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
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    report = analyze_runtime_events(args.events, args.top, args.labels)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
