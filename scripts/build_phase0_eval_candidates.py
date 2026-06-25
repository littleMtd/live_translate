from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.sample_labeling_cases import (
    DEFAULT_AUDIO_ROOT,
    DEFAULT_CONTEXT_TAG_OPTIONS,
    DEFAULT_LOG_DIR,
    DEFAULT_SPEAKER_SOURCE_OPTIONS,
    TARGET_SCHEMA_VERSION,
    _sample_entry,
    _string_list,
    _translation_cut_reason,
    _unique_preserving_order,
    build_event_index,
    build_prior_source_usage,
    build_stt_index,
    latest_event_file,
    read_runtime_rows,
    translation_population,
)


DEFAULT_TOTAL = 100
DEFAULT_RANDOM = 40
DEFAULT_FORCED = 15
DEFAULT_SILENCE = 15
DEFAULT_MULTI = 10
DEFAULT_LOW_CONFIDENCE = 10
DEFAULT_SUSPICIOUS = 10
SUSPICIOUS_FLAGS = {
    "empty_target",
    "very_short_target",
    "low_target_cjk",
    "target_has_hangul",
    "target_high_latin",
    "target_has_japanese",
    "low_source_hangul",
    "repetitive_target",
}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _default_output_path() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return DEFAULT_LOG_DIR / f"labeling_sample_phase0_eval_{stamp}.json"


def _row_key(row: dict[str, Any]) -> int:
    return int(row["line_no"])


def _source_ids(event: dict[str, Any]) -> list[str]:
    return _unique_preserving_order(
        _string_list(event.get("source_utterance_ids"))
        + _string_list(event.get("evidence_source_utterance_ids"))
    )


def _has_audio_and_stt(
    row: dict[str, Any],
    *,
    audio_root: Path,
    stt_index: dict[tuple[str, str], dict[str, Any]],
) -> bool:
    event = row["event"]
    run_id = str(event.get("run_id") or "")
    ids = _source_ids(event)
    if not run_id or not ids:
        return False
    for utterance_id in ids:
        if not (audio_root / run_id / f"{utterance_id}.wav").exists():
            return False
        if (run_id, utterance_id) not in stt_index:
            return False
    return True


def _quality_flags(row: dict[str, Any]) -> set[str]:
    event = row["event"]
    flags = event.get("quality_flags")
    if not isinstance(flags, list):
        return set()
    return {str(flag) for flag in flags}


def _numeric_list(value: Any) -> list[float]:
    if not isinstance(value, list):
        return []
    out: list[float] = []
    for item in value:
        if isinstance(item, (int, float)):
            out.append(float(item))
    return out


def _min_avg_logprob(row: dict[str, Any]) -> float | None:
    value = row["event"].get("min_avg_logprob")
    if isinstance(value, (int, float)):
        return float(value)
    values = _numeric_list(row["event"].get("source_avg_logprobs"))
    return min(values) if values else None


def _max_no_speech_prob(row: dict[str, Any]) -> float | None:
    value = row["event"].get("max_no_speech_prob")
    if isinstance(value, (int, float)):
        return float(value)
    values = _numeric_list(row["event"].get("source_no_speech_probs"))
    return max(values) if values else None


def _is_forced(row: dict[str, Any]) -> bool:
    cut = _translation_cut_reason(row["event"])
    return cut in {"forced_prefix", "forced_blob", "forced_gap_prefix"} or cut.startswith("merged:forced_blob")


def _is_silence(row: dict[str, Any]) -> bool:
    return _translation_cut_reason(row["event"]) == "silence_complete"


def _is_multi_or_evidence(row: dict[str, Any]) -> bool:
    event = row["event"]
    source_ids = _unique_preserving_order(_string_list(event.get("source_utterance_ids")))
    evidence_ids = _unique_preserving_order(_string_list(event.get("evidence_source_utterance_ids")))
    return len(source_ids) > 1 or bool(evidence_ids)


def _is_low_confidence(row: dict[str, Any]) -> bool:
    min_avg = _min_avg_logprob(row)
    max_no_speech = _max_no_speech_prob(row)
    flags = _quality_flags(row)
    return (
        (min_avg is not None and min_avg <= -0.7)
        or (max_no_speech is not None and max_no_speech >= 0.1)
        or "low_source_hangul" in flags
    )


def _is_suspicious(row: dict[str, Any]) -> bool:
    return bool(_quality_flags(row) & SUSPICIOUS_FLAGS)


def _select_bucket(
    *,
    rng: random.Random,
    rows: list[dict[str, Any]],
    selected_keys: set[int],
    quota: int,
    predicate: Callable[[dict[str, Any]], bool],
) -> list[dict[str, Any]]:
    if quota <= 0:
        return []
    pool = [row for row in rows if _row_key(row) not in selected_keys and predicate(row)]
    take = min(quota, len(pool))
    picked = rng.sample(pool, take) if take else []
    selected_keys.update(_row_key(row) for row in picked)
    return picked


def build_phase0_candidates(
    *,
    events_path: Path,
    audio_root: Path = DEFAULT_AUDIO_ROOT,
    run_ids: set[str] | None = None,
    seed: int | None = None,
    total: int = DEFAULT_TOTAL,
    random_count: int = DEFAULT_RANDOM,
    forced_count: int = DEFAULT_FORCED,
    silence_count: int = DEFAULT_SILENCE,
    multi_count: int = DEFAULT_MULTI,
    low_confidence_count: int = DEFAULT_LOW_CONFIDENCE,
    suspicious_count: int = DEFAULT_SUSPICIOUS,
) -> dict[str, Any]:
    if total < 1:
        raise ValueError("total must be positive")
    requested_counts = {
        "random": random_count,
        "forced": forced_count,
        "silence": silence_count,
        "multi": multi_count,
        "low_confidence": low_confidence_count,
        "suspicious": suspicious_count,
    }
    if any(count < 0 for count in requested_counts.values()):
        raise ValueError("bucket counts must be non-negative")
    if sum(requested_counts.values()) > total:
        raise ValueError("requested bucket counts must not exceed total")
    rows = read_runtime_rows(events_path)
    raw_population = translation_population(rows, run_ids)
    stt_index = build_stt_index(rows)
    sentence_index = build_event_index(rows, "sentence")
    audio_index = build_event_index(rows, "audio")
    eligible = [
        row for row in raw_population
        if _has_audio_and_stt(row, audio_root=audio_root, stt_index=stt_index)
    ]
    if len(eligible) < total:
        raise ValueError(f"eligible population too small: {len(eligible)} < {total}")

    selected_seed = seed
    if selected_seed is None:
        selected_seed = random.SystemRandom().randrange(0, 2**63)
    rng = random.Random(selected_seed)
    selected_keys: set[int] = set()
    buckets: list[tuple[str, list[dict[str, Any]], int]] = []
    bucket_specs: list[tuple[str, int, Callable[[dict[str, Any]], bool]]] = [
        ("forced_cut", forced_count, _is_forced),
        ("silence_complete", silence_count, _is_silence),
        ("multi_or_evidence", multi_count, _is_multi_or_evidence),
        ("low_confidence", low_confidence_count, _is_low_confidence),
        ("quality_suspicious", suspicious_count, _is_suspicious),
    ]
    for bucket, quota, predicate in bucket_specs:
        picked = _select_bucket(
            rng=rng,
            rows=eligible,
            selected_keys=selected_keys,
            quota=quota,
            predicate=predicate,
        )
        buckets.append((bucket, picked, quota))

    remaining_for_random = max(0, min(random_count, total - sum(len(rows) for _, rows, _ in buckets)))
    random_picked = _select_bucket(
        rng=rng,
        rows=eligible,
        selected_keys=selected_keys,
        quota=remaining_for_random,
        predicate=lambda _row: True,
    )
    buckets.append(("random_holdout", random_picked, random_count))

    selected_pairs: list[tuple[str, dict[str, Any]]] = [
        (bucket, row)
        for bucket, rows_for_bucket, _quota in buckets
        for row in rows_for_bucket
    ]
    if len(selected_pairs) < total:
        fill_rows = _select_bucket(
            rng=rng,
            rows=eligible,
            selected_keys=selected_keys,
            quota=total - len(selected_pairs),
            predicate=lambda _row: True,
        )
        buckets.append(("fill_random", fill_rows, total - len(selected_pairs)))
        selected_pairs.extend(("fill_random", row) for row in fill_rows)

    prior_source_usage = build_prior_source_usage(raw_population)
    samples = []
    for index, (bucket, row) in enumerate(selected_pairs[:total], start=1):
        sample = _sample_entry(
            sample_index=index,
            row=row,
            audio_root=audio_root,
            stt_index=stt_index,
            sentence_index=sentence_index,
            audio_index=audio_index,
            prior_source_usage=prior_source_usage,
        )
        sample["phase0_bucket"] = bucket
        samples.append(sample)

    bucket_counts = {bucket: len(rows_for_bucket) for bucket, rows_for_bucket, _quota in buckets}
    bucket_shortfalls = {
        bucket: max(0, quota - len(rows_for_bucket))
        for bucket, rows_for_bucket, quota in buckets
        if quota > len(rows_for_bucket)
    }
    missing_audio = [
        {"sample_id": sample["sample_id"], "utterance_id": chunk["utterance_id"], "audio_path": chunk["audio_path"]}
        for sample in samples
        for chunk in sample["source_chunks"]
        if not chunk["audio_exists"]
    ]
    missing_stt_events = [
        {"sample_id": sample["sample_id"], "utterance_id": chunk["utterance_id"]}
        for sample in samples
        for chunk in sample["source_chunks"]
        if not chunk["stt_event_found"]
    ]
    return {
        "labeling_sample_schema": 1,
        "speaker_policy": "host-primary",
        "annotation_goal": (
            "Phase 0 eval candidates under host-primary policy: host speech has priority; "
            "when host is silent, clear clip/game/other speech can be valid source."
        ),
        "annotation_rules": [
            "Host-primary: if host/main speaker is audible, judge against host speech.",
            "If host is silent and clip/game/other speaker is the only clear speech, judge against that speech.",
            "If the subtitle translates clip/game/other-speaker audio while host is speaking, mark a speaker/source error tag.",
            "For host_over_clip overlap, treat the host speech as the correct source.",
            "Host silence means no intelligible host speech in the relevant chunks; quiet words, short phrases, clear fillers, or overlap still count as host audible.",
            "Laughter, humming, breathing, or unintelligible murmurs do not by themselves count as host speech; use speaker_unclear or unclear if unsure.",
            "Report host-speech, clip-when-host-silent, overlap/wrong-speaker, and speaker-unclear cases separately; do not mix their rescue rates.",
            "Listen to every source_chunks[].audio_path before assigning b_stt_error.",
            "Treat source_utterance_ids as current source; evidence_source_utterance_ids preserves carry-forward evidence.",
            "Use this file as a candidate pool; do not estimate global rates from this stratified sample.",
        ],
        "label_options": [
            "a_translation_error",
            "b_stt_error",
            "both",
            "ok",
            "unclear",
        ],
        "context_tag_options": DEFAULT_CONTEXT_TAG_OPTIONS,
        "speaker_source_options": DEFAULT_SPEAKER_SOURCE_OPTIONS,
        "sampling": {
            "method": "phase0_stratified_candidates",
            "events_path": str(events_path.resolve(strict=False)),
            "audio_root": str(audio_root.resolve(strict=False)),
            "schema_version": TARGET_SCHEMA_VERSION,
            "event_type": "translation",
            "run_ids": sorted(run_ids) if run_ids is not None else [],
            "seed": selected_seed,
            "raw_population_size": len(raw_population),
            "eligible_population_size": len(eligible),
            "sample_size": len(samples),
            "requested_total": total,
            "requested_buckets": {
                "random_holdout": random_count,
                "forced_cut": forced_count,
                "silence_complete": silence_count,
                "multi_or_evidence": multi_count,
                "low_confidence": low_confidence_count,
                "quality_suspicious": suspicious_count,
            },
            "bucket_counts": bucket_counts,
            "bucket_shortfalls": bucket_shortfalls,
        },
        "quality_control": {
            "missing_audio_files": missing_audio,
            "missing_stt_events": missing_stt_events,
        },
        "samples": samples,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Phase 0 host-primary eval candidate samples.")
    parser.add_argument("--events", type=Path, default=None, help="Path to runtime_events_YYYYMMDD.jsonl.")
    parser.add_argument("--audio-root", type=Path, default=DEFAULT_AUDIO_ROOT, help="Path to logs/audio_dump.")
    parser.add_argument("--output", type=Path, default=None, help="Output labeling_sample JSON path.")
    parser.add_argument("--run-id", action="append", default=None, help="Restrict to this exact run_id. Repeatable.")
    parser.add_argument("--seed", type=int, default=None, help="Random seed for reproducible sampling.")
    parser.add_argument("--total", type=int, default=DEFAULT_TOTAL, help="Total candidate sample count.")
    parser.add_argument("--random", type=int, default=DEFAULT_RANDOM, help="Random hold-out candidate count.")
    parser.add_argument("--forced", type=int, default=DEFAULT_FORCED, help="Forced cut candidate count.")
    parser.add_argument("--silence", type=int, default=DEFAULT_SILENCE, help="Silence-complete candidate count.")
    parser.add_argument("--multi", type=int, default=DEFAULT_MULTI, help="Multi-source/evidence candidate count.")
    parser.add_argument("--low-confidence", type=int, default=DEFAULT_LOW_CONFIDENCE, help="Low-confidence candidate count.")
    parser.add_argument("--suspicious", type=int, default=DEFAULT_SUSPICIOUS, help="Quality-flag suspicious candidate count.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    events_path = args.events or latest_event_file()
    if events_path is None:
        print("No runtime_events_*.jsonl file found.", file=sys.stderr)
        return 1
    output_path = args.output or _default_output_path()
    try:
        data = build_phase0_candidates(
            events_path=events_path,
            audio_root=args.audio_root,
            run_ids=set(args.run_id) if args.run_id else None,
            seed=args.seed,
            total=args.total,
            random_count=args.random,
            forced_count=args.forced,
            silence_count=args.silence,
            multi_count=args.multi,
            low_confidence_count=args.low_confidence,
            suspicious_count=args.suspicious,
        )
    except ValueError as exc:
        print(f"Phase 0 candidate build failed: {exc}", file=sys.stderr)
        return 2
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        "Wrote {samples} Phase 0 candidates from eligible population {population} to {path} "
        "(seed={seed})".format(
            samples=data["sampling"]["sample_size"],
            population=data["sampling"]["eligible_population_size"],
            path=output_path,
            seed=data["sampling"]["seed"],
        )
    )
    print(f"Bucket counts: {data['sampling']['bucket_counts']}")
    if data["sampling"]["bucket_shortfalls"]:
        print(f"Bucket shortfalls: {data['sampling']['bucket_shortfalls']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
