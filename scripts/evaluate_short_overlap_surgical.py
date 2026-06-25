"""Surgical short-overlap dedupe: window-definition sweep (offline, read-only).

Item 3 of DETERMINISTIC_FIXES_PROPOSAL_20260624.md. The existing
scripts/evaluate_short_overlap_dedupe_shadow.py lowers the global
`min_overlap_chars` to 4 (gated on overlap_seconds>0) and finds 24 candidates over
0613-0619. This script reuses its event loader and sweeps several "overlap region"
definitions over ALL sessions so the boundary choice is data-driven, not silently
fixed:

- blunt char threshold (min_overlap_chars in {4, 3});
- a surgical cap: only dedupe when the removed prefix is short enough to physically
  fit in the reported overlap_seconds (removed Hangul length <= overlap_seconds * CPS),
  which protects intentional long repetitions.

It reports blast radius per definition and emits the candidate list with audio_dump
paths for the human audio-review gate. It does not modify the live dedupe path.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules.stt_policy import dedupe_transcript_overlap  # noqa: E402

_HANGUL = re.compile(r"[가-힣]")


def load_runtime_events(paths):
    """Tolerant loader (live logs get truncated): skip unparseable lines.

    Returns ({(run_id, utterance_id): stt_event}, [translation_events]).
    """
    stt_events: dict[tuple[str, str], dict] = {}
    translations: list[dict] = []
    for path in paths:
        path = Path(path)
        with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
            for line_no, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(event, dict):
                    continue
                run_id = str(event.get("run_id") or "")
                if event.get("event_type") == "stt":
                    uid = str(event.get("utterance_id") or "")
                    if run_id and uid:
                        stt_events[(run_id, uid)] = event
                elif event.get("event_type") == "translation" and event.get("status") == "success":
                    translations.append(event)
    return stt_events, translations
# Conservative fast-Korean rate; generous so the cap protects only clearly-too-long
# removals. Documented as an assumption to verify, not a measured constant.
DEFAULT_CHARS_PER_SECOND = 7.0


def _hangul_len(text: str) -> int:
    return len(_HANGUL.findall(text or ""))


def _removed_prefix(previous: str, current: str, min_chars: int) -> str:
    """Prefix that dedupe_transcript_overlap would remove at this min_chars (\"\" if none)."""
    result = dedupe_transcript_overlap(previous, current, min_overlap_chars=min_chars)
    if result == current:
        return ""
    return current[: len(current) - len(result)].rstrip()


def find_candidates(stt_events, translations, *, cps: float) -> list[dict]:
    by_run: dict[str, list[dict]] = {}
    for event in translations:
        run_id = str(event.get("run_id") or "")
        if run_id and isinstance(event.get("sequence_id"), int):
            by_run.setdefault(run_id, []).append(event)

    out: list[dict] = []
    for run_id, events in by_run.items():
        events.sort(key=lambda item: item["sequence_id"])
        for previous, current in zip(events, events[1:]):
            if current["sequence_id"] != previous["sequence_id"] + 1:
                continue
            prev_text = str(previous.get("source_text") or "").strip()
            cur_text = str(current.get("source_text") or "").strip()
            src_ids = [str(v) for v in (current.get("source_utterance_ids") or [])]
            previous_src_ids = [
                str(v) for v in (previous.get("source_utterance_ids") or [])
            ]
            if not prev_text or not cur_text or not src_ids:
                continue
            if not previous_src_ids:
                continue
            first_uid = src_ids[0]
            previous_last_uid = previous_src_ids[-1]
            stt = stt_events.get((run_id, first_uid), {})
            overlap_seconds = float(stt.get("overlap_seconds") or 0.0)
            if overlap_seconds <= 0:
                continue

            # Default (min_chars=5) must NOT already dedupe, else it is not a new candidate.
            if dedupe_transcript_overlap(prev_text, cur_text) != cur_text:
                continue
            removed4 = _removed_prefix(prev_text, cur_text, 4)
            removed3 = _removed_prefix(prev_text, cur_text, 3)
            # the most permissive removal defines the candidate
            removed = removed3 or removed4
            if not removed:
                continue
            removed_hangul = _hangul_len(removed)
            cap = math.ceil(overlap_seconds * cps) + 1  # Hangul chars the overlap could hold

            definitions = {
                "blunt_min4": bool(removed4),
                "aggressive_min3": bool(removed3),
                "surgical_min4_capped": bool(removed4) and removed_hangul <= cap,
                "surgical_min3_capped": bool(removed3) and removed_hangul <= cap,
            }
            previous_audio_path = f"logs/audio_dump/{run_id}/{previous_last_uid}.wav"
            current_audio_path = f"logs/audio_dump/{run_id}/{first_uid}.wav"
            out.append({
                "run_id": run_id,
                "profile_id": current.get("profile_id"),
                "sequence_id": current["sequence_id"],
                "previous_sequence_id": previous["sequence_id"],
                "overlap_seconds": overlap_seconds,
                "removed_prefix": removed,
                "removed_prefix_min4": removed4,
                "removed_prefix_min3": removed3,
                "shadow_source_text_min4": (
                    dedupe_transcript_overlap(prev_text, cur_text, min_overlap_chars=4)
                    if removed4 else cur_text
                ),
                "shadow_source_text_min3": (
                    dedupe_transcript_overlap(prev_text, cur_text, min_overlap_chars=3)
                    if removed3 else cur_text
                ),
                "removed_total_chars": len(removed),
                "removed_hangul_chars": removed_hangul,
                "overlap_char_cap": cap,
                "fits_overlap_window": removed_hangul <= cap,
                "previous_source_text": prev_text,
                "current_source_text": cur_text,
                "previous_last_source_utterance_id": previous_last_uid,
                "first_source_utterance_id": first_uid,
                "previous_audio_path": previous_audio_path,
                "current_audio_path": current_audio_path,
                # Retained for compatibility with the first shadow artifact.
                "audio_path": current_audio_path,
                "previous_audio_exists": (PROJECT_ROOT / previous_audio_path).is_file(),
                "current_audio_exists": (PROJECT_ROOT / current_audio_path).is_file(),
                "definitions": definitions,
                "review_disposition": "unreviewed",
            })
    return out


def build_report(paths: list[Path], *, cps: float) -> dict:
    stt_events, translations = load_runtime_events(paths)
    candidates = find_candidates(stt_events, translations, cps=cps)
    def _count(key): return sum(1 for c in candidates if c["definitions"][key])
    blast = {key: _count(key) for key in
             ("blunt_min4", "aggressive_min3", "surgical_min4_capped", "surgical_min3_capped")}
    review_candidates = [candidate for candidate in candidates if candidate["definitions"]["blunt_min4"]]
    missing_review_audio = [
        {
            "run_id": candidate["run_id"],
            "sequence_id": candidate["sequence_id"],
            "previous_audio_exists": candidate["previous_audio_exists"],
            "current_audio_exists": candidate["current_audio_exists"],
        }
        for candidate in review_candidates
        if not candidate["previous_audio_exists"] or not candidate["current_audio_exists"]
    ]
    return {
        "short_overlap_surgical_schema": 1,
        "regenerate_command": (
            "live-subtitle-env\\Scripts\\python.exe scripts\\evaluate_short_overlap_surgical.py "
            "--events \"logs/runtime_events_202605*.jsonl\" "
            "\"logs/runtime_events_2026060*.jsonl\" "
            "\"logs/runtime_events_2026061*.jsonl\" "
            "\"logs/runtime_events_2026062[0-4].jsonl\""
        ),
        "live_path_changed": False,
        "chars_per_second_assumption": cps,
        "translation_count": len(translations),
        "candidate_union_count": len(candidates),
        "review_candidate_definition": "blunt_min4",
        "review_candidate_count": len(review_candidates),
        "review_audio_complete_count": len(review_candidates) - len(missing_review_audio),
        "missing_review_audio": missing_review_audio,
        "blast_radius_by_definition": blast,
        "definition_notes": {
            "blunt_min4": "existing shadow rule: min_overlap_chars=4, gated on overlap>0.",
            "aggressive_min3": "min_overlap_chars=3; larger blast, more false-deletion risk.",
            "surgical_min4_capped": "min4 AND removed Hangul length <= overlap_seconds*CPS+1; "
                                    "protects intentional long repetitions.",
            "surgical_min3_capped": "min3 AND the same overlap-window cap.",
        },
        "profile_counts": dict(sorted(Counter(str(c["profile_id"]) for c in candidates).items())),
        "candidates": candidates,
        "human_review_command": (
            "live-subtitle-env\\Scripts\\python.exe "
            "scripts\\short_overlap_dedupe_review_server.py"
        ),
        "human_review_required": "Audio-review both previous_audio_path and "
                                 "current_audio_path for every blunt_min4 candidate before "
                                 "any live change. Gate: zero confirmed host-content deletions.",
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sweep surgical short-overlap dedupe definitions.")
    parser.add_argument("--events", nargs="+", required=True)
    parser.add_argument("--cps", type=float, default=DEFAULT_CHARS_PER_SECOND)
    parser.add_argument("--output", type=Path,
                        default=PROJECT_ROOT / ".analysis-tmp" / "short_overlap_surgical_20260624.json")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    paths: list[Path] = []
    for pattern in args.events:
        paths.extend(Path(p).resolve() for p in sorted(glob.glob(pattern)))
    if not paths:
        print("no event files matched", file=sys.stderr)
        return 1
    report = build_report(paths, cps=args.cps)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {args.output} | union={report['candidate_union_count']} "
          f"blast={report['blast_radius_by_definition']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
