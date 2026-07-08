from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules.stt_policy import dedupe_transcript_overlap


DEFAULT_OUTPUT = PROJECT_ROOT / "scratch" / "analysis" / "phase0_short_overlap_dedupe_shadow_20260624.json"


def load_runtime_events(paths: Iterable[Path]) -> tuple[dict[tuple[str, str], dict[str, Any]], list[dict[str, Any]]]:
    stt_events: dict[tuple[str, str], dict[str, Any]] = {}
    translations: list[dict[str, Any]] = []
    for path in paths:
        with path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid JSON at {path}:{line_no}: {exc}") from exc
                run_id = str(event.get("run_id") or "")
                event_type = event.get("event_type")
                if event_type == "stt":
                    utterance_id = str(event.get("utterance_id") or "")
                    if run_id and utterance_id:
                        stt_events[(run_id, utterance_id)] = event
                elif event_type == "translation" and event.get("status") == "success":
                    copy = dict(event)
                    copy["_runtime_path"] = str(path.relative_to(PROJECT_ROOT))
                    copy["_runtime_line"] = line_no
                    translations.append(copy)
    return stt_events, translations


def find_short_overlap_candidates(
    stt_events: dict[tuple[str, str], dict[str, Any]],
    translations: Iterable[dict[str, Any]],
    *,
    relaxed_min_chars: int = 4,
) -> list[dict[str, Any]]:
    by_run: dict[str, list[dict[str, Any]]] = {}
    for event in translations:
        run_id = str(event.get("run_id") or "")
        if run_id and isinstance(event.get("sequence_id"), int):
            by_run.setdefault(run_id, []).append(event)

    candidates: list[dict[str, Any]] = []
    for run_id, events in by_run.items():
        events.sort(key=lambda item: item["sequence_id"])
        for previous, current in zip(events, events[1:]):
            if current["sequence_id"] != previous["sequence_id"] + 1:
                continue
            previous_text = str(previous.get("source_text") or "").strip()
            current_text = str(current.get("source_text") or "").strip()
            previous_source_ids = [str(value) for value in (previous.get("source_utterance_ids") or [])]
            source_ids = current.get("source_utterance_ids") or []
            if not previous_text or not current_text or not previous_source_ids or not source_ids:
                continue
            current_source_ids = [str(value) for value in source_ids]
            first_utterance_id = current_source_ids[0]
            stt = stt_events.get((run_id, first_utterance_id), {})
            overlap_seconds = float(stt.get("overlap_seconds") or 0.0)
            if overlap_seconds <= 0:
                continue

            default_result = dedupe_transcript_overlap(previous_text, current_text)
            relaxed_result = dedupe_transcript_overlap(
                previous_text,
                current_text,
                min_overlap_chars=relaxed_min_chars,
            )
            if default_result != current_text or relaxed_result == current_text:
                continue
            removed_length = len(current_text) - len(relaxed_result)
            candidates.append(
                {
                    "run_id": run_id,
                    "profile_id": current.get("profile_id"),
                    "previous_sequence_id": previous["sequence_id"],
                    "sequence_id": current["sequence_id"],
                    "previous_source_utterance_ids": previous_source_ids,
                    "current_source_utterance_ids": current_source_ids,
                    "previous_last_source_utterance_id": previous_source_ids[-1],
                    "first_source_utterance_id": first_utterance_id,
                    "overlap_seconds": overlap_seconds,
                    "removed_prefix": current_text[:removed_length].rstrip(),
                    "previous_source_text": previous_text,
                    "current_source_text": current_text,
                    "shadow_source_text": relaxed_result,
                    "runtime_path": current.get("_runtime_path"),
                    "runtime_line": current.get("_runtime_line"),
                    "review_disposition": "unreviewed",
                }
            )
    return candidates


def build_report(paths: list[Path], relaxed_min_chars: int = 4) -> dict[str, Any]:
    stt_events, translations = load_runtime_events(paths)
    candidates = find_short_overlap_candidates(
        stt_events,
        translations,
        relaxed_min_chars=relaxed_min_chars,
    )
    return {
        "phase0_short_overlap_dedupe_shadow_schema": 1,
        "live_path_changed": False,
        "runtime_files": [str(path.relative_to(PROJECT_ROOT)) for path in paths],
        "translation_count": len(translations),
        "relaxed_min_overlap_chars": relaxed_min_chars,
        "candidate_count": len(candidates),
        "profile_counts": dict(sorted(Counter(str(item["profile_id"]) for item in candidates).items())),
        "candidates": candidates,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Shadow a shorter transcript-overlap dedupe threshold.")
    parser.add_argument("runtime_files", nargs="*", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--min-chars", type=int, default=4)
    args = parser.parse_args()

    paths = args.runtime_files or sorted((PROJECT_ROOT / "logs").glob("runtime_events_2026061[3-9].jsonl"))
    report = build_report([path.resolve() for path in paths], args.min_chars)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.output}: {report['candidate_count']} candidates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
