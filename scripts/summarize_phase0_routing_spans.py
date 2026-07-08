from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.routing_span_annotations import (
    RoutingAnnotationStore,
    build_routing_tasks,
    coverage_gaps,
    load_manifest,
    normalize_spans,
    sha256_file,
)


DEFAULT_MANIFEST = PROJECT_ROOT / "scratch" / "analysis" / "phase0_replay_manifest_20260624.json"
DEFAULT_ANNOTATIONS = PROJECT_ROOT / "scratch" / "analysis" / "phase0_routing_spans_20260624.annotations.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "scratch" / "analysis" / "phase0_routing_span_summary_20260624.json"


def _mechanism(spans: list[dict[str, Any]]) -> str:
    classes = {span["source_class"] for span in spans if span["source_class"] != "uncertain"}
    if "mixed" in classes:
        return "overlap_extraction"
    if "alert_tts" in classes:
        return "sequential_alert_suppression"
    if classes == {"host"}:
        return "host_only_no_audio_routing"
    if classes <= {"unrelated"}:
        return "suppress_non_speech"
    return "other"


def build_summary(
    *,
    manifest_path: Path,
    annotation_path: Path,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    tasks = build_routing_tasks(manifest, project_root=project_root)
    store = RoutingAnnotationStore(
        path=annotation_path,
        manifest_path=manifest_path,
        tasks=tasks,
    )
    annotations = store.snapshot()["annotations"]
    expected_ids = {str(task["sample_id"]) for task in tasks}
    if set(annotations) != expected_ids:
        missing = sorted(expected_ids - set(annotations))
        extra = sorted(set(annotations) - expected_ids)
        raise ValueError(f"routing annotation set mismatch: missing={missing}, extra={extra}")

    class_seconds: defaultdict[str, float] = defaultdict(float)
    action_seconds: defaultdict[str, float] = defaultdict(float)
    pair_seconds: defaultdict[str, float] = defaultdict(float)
    mechanism_counts: Counter[str] = Counter()
    cases: list[dict[str, Any]] = []

    for task in tasks:
        sample_id = str(task["sample_id"])
        annotation = annotations[sample_id]
        if annotation.get("status") != "complete":
            raise ValueError(f"routing annotation is not complete: {sample_id}")
        case_spans: list[dict[str, Any]] = []
        for asset in task["audio_assets"]:
            utterance_id = str(asset["utterance_id"])
            duration = float(asset["duration_seconds"])
            raw = annotation["assets"].get(utterance_id)
            if not isinstance(raw, dict):
                raise ValueError(f"missing asset annotation: {sample_id}/{utterance_id}")
            spans = normalize_spans(raw.get("spans"), duration_seconds=duration)
            gaps = coverage_gaps(spans, duration_seconds=duration)
            if gaps:
                raise ValueError(f"routing coverage gaps: {sample_id}/{utterance_id}: {gaps}")
            for span in spans:
                enriched = dict(span)
                enriched["utterance_id"] = utterance_id
                case_spans.append(enriched)
                seconds = float(span["end_seconds"]) - float(span["start_seconds"])
                class_seconds[span["source_class"]] += seconds
                action_seconds[span["routing_action"]] += seconds
                pair_seconds[f"{span['source_class']}|{span['routing_action']}"] += seconds

        mechanism = _mechanism(case_spans)
        mechanism_counts[mechanism] += 1
        cases.append(
            {
                "sample_id": sample_id,
                "mechanism": mechanism,
                "audio_asset_count": len(task["audio_assets"]),
                "span_count": len(case_spans),
                "uncertain_seconds": round(
                    sum(
                        span["end_seconds"] - span["start_seconds"]
                        for span in case_spans
                        if span["source_class"] == "uncertain"
                    ),
                    3,
                ),
                "spans": case_spans,
            }
        )

    total_seconds = sum(class_seconds.values())
    excluded_seconds = class_seconds.get("uncertain", 0.0)
    return {
        "phase0_routing_span_summary_schema": 1,
        "manifest_path": str(manifest_path),
        "manifest_sha256": sha256_file(manifest_path),
        "annotation_path": str(annotation_path),
        "annotation_sha256": sha256_file(annotation_path),
        "case_count": len(cases),
        "audio_asset_count": sum(case["audio_asset_count"] for case in cases),
        "span_count": sum(case["span_count"] for case in cases),
        "total_seconds": round(total_seconds, 3),
        "evaluable_seconds": round(total_seconds - excluded_seconds, 3),
        "excluded_uncertain_seconds": round(excluded_seconds, 3),
        "excluded_uncertain_ratio": round(excluded_seconds / total_seconds, 6),
        "source_class_seconds": {
            key: round(value, 3) for key, value in sorted(class_seconds.items())
        },
        "routing_action_seconds": {
            key: round(value, 3) for key, value in sorted(action_seconds.items())
        },
        "class_action_seconds": {
            key: round(value, 3) for key, value in sorted(pair_seconds.items())
        },
        "mechanism_counts": dict(sorted(mechanism_counts.items())),
        "cases": cases,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate and summarize Phase 0 routing spans.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--annotations", type=Path, default=DEFAULT_ANNOTATIONS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        summary = build_summary(
            manifest_path=args.manifest,
            annotation_path=args.annotations,
        )
    except ValueError as exc:
        print(f"Routing span summary failed: {exc}", file=sys.stderr)
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"Wrote {summary['case_count']} routing cases / {summary['audio_asset_count']} WAV / "
        f"{summary['evaluable_seconds']:.3f}s evaluable to {args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
