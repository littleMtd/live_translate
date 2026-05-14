from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from statistics import fmean
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG_DIR = PROJECT_ROOT / "logs"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def analyze_runtime_events(path: Path | None = None, top_n: int = 10) -> dict[str, Any]:
    event_path = path or latest_event_file(DEFAULT_LOG_DIR)
    if event_path is None or not event_path.exists():
        return {
            "event_path": str(event_path) if event_path else "",
            "available": False,
            "reason": "runtime event file does not exist",
        }

    events = list(_read_events(event_path))
    translation_events = [event for event in events if event.get("event_type") == "translation"]
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
        "latency_ms": _latency_summary(latencies),
        "latest": _latest_samples(translation_events, top_n),
        "flagged_samples": _flagged_samples(translation_events, top_n),
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


def _sample(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "created_at": event.get("created_at"),
        "status": event.get("status"),
        "result_source": event.get("result_source"),
        "cache_status": event.get("cache_status"),
        "engine": event.get("engine"),
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
    print(f"Events: {report['total_events']} | Translations: {report['translation_events']}")
    print(f"Run IDs: {', '.join(report['run_ids'])}")
    print(f"Status breakdown: {report['status_breakdown']}")
    print(f"Latency ms: {report['latency_ms']}")
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
    parser.add_argument("--top", type=int, default=10, help="Number of sample rows per report section.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    report = analyze_runtime_events(args.events, args.top)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
