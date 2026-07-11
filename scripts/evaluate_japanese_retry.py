from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG_DIR = PROJECT_ROOT / "logs"
DEFAULT_OUTPUT = PROJECT_ROOT / "scratch" / "analysis" / "japanese_retry_gate_20260711.json"


def _events(paths: Iterable[Path]) -> Iterable[dict[str, Any]]:
    for path in paths:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict) and event.get("event_type") == "translation":
                    yield event


def evaluate(events: Iterable[dict[str, Any]], labels: dict[str, str] | None = None) -> dict[str, Any]:
    labels = labels or {}
    historical = []
    shadow = []
    for event in events:
        flags = event.get("quality_flags") or []
        if "target_has_japanese" in flags:
            historical.append(event)
        retry = event.get("quality_retry")
        if isinstance(retry, dict) and retry.get("trigger") == "target_has_japanese":
            shadow.append((event, retry))
    verdicts = Counter(labels.values())
    labeled = sum(verdicts[value] for value in ("better", "equivalent", "worse"))
    requirements = {
        "minimum_shadow_events": len(shadow) >= 30,
        "minimum_semantic_labels": labeled >= 30,
        "zero_observed_false_corrections": verdicts["worse"] == 0 and labeled > 0,
        "strict_qe_improvement_rate_ge_0_5": (
            sum(bool(retry.get("would_replace")) for _, retry in shadow) / len(shadow) >= 0.5
            if shadow
            else False
        ),
    }
    samples = [
        {
            "created_at": event.get("created_at"),
            "source_text": event.get("source_text"),
            "target_text": event.get("target_text"),
            "profile_id": event.get("profile_id"),
            "quality_severity": event.get("quality_severity"),
        }
        for event in historical[-20:]
    ]
    return {
        "schema": 1,
        "historical_japanese_flag_events": len(historical),
        "historical_by_profile": dict(Counter(str(e.get("profile_id") or "unknown") for e in historical)),
        "shadow_events": len(shadow),
        "shadow_would_replace": sum(bool(retry.get("would_replace")) for _, retry in shadow),
        "semantic_labels": dict(verdicts),
        "gate_requirements": requirements,
        "active_mode_decision": "go" if all(requirements.values()) else "no-go",
        "decision_reason": (
            "Reference-free Japanese-script detection cannot distinguish a leak from a valid "
            "Japanese name, quote, or stylized term; active replacement requires shadow data "
            "and human semantic labels."
        ),
        "samples": samples,
    }


def _labels(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("labels must be a JSON object mapping event IDs to verdicts")
    return {str(key): str(value) for key, value in data.items()}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate the Japanese quality-retry activation gate.")
    parser.add_argument("--logs", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--labels", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        report = evaluate(
            _events(sorted(args.logs.glob("runtime_events_*.jsonl"))),
            _labels(args.labels),
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Japanese retry evaluation failed: {exc}", file=sys.stderr)
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"Japanese retry: historical={report['historical_japanese_flag_events']} "
        f"shadow={report['shadow_events']} decision={report['active_mode_decision']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
