from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import fmean
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG_DIR = PROJECT_ROOT / "logs"
DEFAULT_AUDIO_ROOT = DEFAULT_LOG_DIR / "audio_dump"
TARGET_SCHEMA_VERSION = 2
DEFAULT_MIN_POPULATION = 150
DEFAULT_TOP_N = 10
DEFAULT_LONG_TEXT_CHARS = 180

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def latest_event_file(log_dir: Path = DEFAULT_LOG_DIR) -> Path | None:
    files = sorted(log_dir.glob("runtime_events_*.jsonl"), key=lambda path: path.stat().st_mtime)
    return files[-1] if files else None


def read_runtime_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                rows.append({"line_no": line_no, "event": event})
    return rows


def build_collection_sanity_report(
    *,
    events_path: Path,
    audio_root: Path = DEFAULT_AUDIO_ROOT,
    run_ids: set[str] | None = None,
    min_population: int = DEFAULT_MIN_POPULATION,
    top_n: int = DEFAULT_TOP_N,
    long_text_chars: int = DEFAULT_LONG_TEXT_CHARS,
) -> dict[str, Any]:
    if min_population < 0:
        raise ValueError("min_population must be non-negative")
    if top_n < 1:
        raise ValueError("top_n must be positive")
    if long_text_chars < 1:
        raise ValueError("long_text_chars must be positive")
    if not events_path.exists():
        return {
            "collection_sanity_schema": 1,
            "available": False,
            "events_path": str(events_path.resolve(strict=False)),
            "audio_root": str(audio_root.resolve(strict=False)),
            "reason": "runtime event file does not exist",
        }

    rows = read_runtime_rows(events_path)
    schema2_rows = [row for row in rows if row["event"].get("schema_version") == TARGET_SCHEMA_VERSION]
    scoped_schema2_rows = _filter_rows_by_run(schema2_rows, run_ids)
    translations = [
        row for row in scoped_schema2_rows
        if row["event"].get("event_type") == "translation"
    ]
    stt_rows = [
        row for row in scoped_schema2_rows
        if row["event"].get("event_type") == "stt"
    ]
    audio_rows = [
        row for row in scoped_schema2_rows
        if row["event"].get("event_type") == "audio"
    ]
    stt_index = _build_stt_index(stt_rows)

    join_quality = _join_quality(translations, stt_index, audio_root, top_n)
    profiles = _counter(translations, "profile_id")
    status_breakdown = _counter(translations, "status")
    run_summaries = _run_summaries(translations, stt_rows, audio_rows, audio_root)
    suspicious = _suspicious_samples(translations, top_n, long_text_chars)
    confidence_summary = _confidence_summary(stt_rows)
    evidence_failures = (
        join_quality["missing_source_id_translations"]
        + join_quality["missing_stt_event_refs"]
        + join_quality["missing_audio_file_refs"]
        + join_quality["missing_confidence_refs"]
        + join_quality["source_confidence_diagnostic_issues"]
    )
    known_profile_count = len([item for item in profiles if item["value"] != "unknown"])
    unknown_profile_events = sum(item["count"] for item in profiles if item["value"] == "unknown")
    profile_ready = known_profile_count == 1 and unknown_profile_events == 0
    ready_for_sampling = (
        len(translations) >= min_population
        and evidence_failures == 0
        and profile_ready
    )
    recommendations = _recommendations(
        translations=translations,
        min_population=min_population,
        evidence_failures=evidence_failures,
        known_profile_count=known_profile_count,
        unknown_profile_events=unknown_profile_events,
        suspicious=suspicious,
        run_ids=run_ids,
        run_summaries=run_summaries,
    )

    return {
        "collection_sanity_schema": 1,
        "available": True,
        "events_path": str(events_path.resolve(strict=False)),
        "audio_root": str(audio_root.resolve(strict=False)),
        "schema_version": TARGET_SCHEMA_VERSION,
        "run_ids_filter": sorted(run_ids) if run_ids is not None else [],
        "ready_for_sampling": ready_for_sampling,
        "sampling_gate": {
            "min_population": min_population,
            "population_size": len(translations),
            "passed": len(translations) >= min_population,
        },
        "counts": {
            "total_rows": len(rows),
            "schema2_rows": len(schema2_rows),
            "scoped_schema2_rows": len(scoped_schema2_rows),
            "translation_events": len(translations),
            "stt_events": len(stt_rows),
            "audio_events": len(audio_rows),
        },
        "profiles": profiles,
        "profile_ready": profile_ready,
        "status_breakdown": status_breakdown,
        "run_summaries": run_summaries,
        "join_quality": join_quality,
        "stt_evidence": confidence_summary,
        "suspicious": suspicious,
        "recommendations": recommendations,
    }


def _filter_rows_by_run(rows: list[dict[str, Any]], run_ids: set[str] | None) -> list[dict[str, Any]]:
    if run_ids is None:
        return rows
    return [
        row for row in rows
        if str(row["event"].get("run_id") or "") in run_ids
    ]


def _build_stt_index(stt_rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for row in stt_rows:
        event = row["event"]
        run_id = str(event.get("run_id") or "")
        utterance_id = str(event.get("utterance_id") or "")
        if run_id and utterance_id:
            index.setdefault((run_id, utterance_id), row)
    return index


def _join_quality(
    translations: list[dict[str, Any]],
    stt_index: dict[tuple[str, str], dict[str, Any]],
    audio_root: Path,
    top_n: int,
) -> dict[str, Any]:
    missing_source_id_samples: list[dict[str, Any]] = []
    missing_stt_events: list[dict[str, Any]] = []
    missing_audio_files: list[dict[str, Any]] = []
    missing_confidence: list[dict[str, Any]] = []
    missing_audio_seconds: list[dict[str, Any]] = []
    duplicate_source_id_samples: list[dict[str, Any]] = []
    source_confidence_diagnostic_samples: list[dict[str, Any]] = []
    source_confidence_issue_counts: Counter[str] = Counter()
    source_ref_count = 0
    unique_source_ref_count = 0
    duplicate_source_ref_count = 0
    multi_chunk_translations = 0
    audio_join_ok_translations = 0

    for row in translations:
        event = row["event"]
        run_id = str(event.get("run_id") or "")
        source_ids = _string_list(event.get("source_utterance_ids"))
        unique_source_ids = _unique_preserving_order(source_ids)
        source_ref_count += len(source_ids)
        unique_source_ref_count += len(unique_source_ids)
        duplicate_count = len(source_ids) - len(unique_source_ids)
        duplicate_source_ref_count += duplicate_count
        if len(unique_source_ids) > 1:
            multi_chunk_translations += 1
        if duplicate_count:
            duplicate_source_id_samples.append(
                {**_sample(row), "duplicate_source_refs": duplicate_count}
            )
        if not source_ids:
            missing_source_id_samples.append(_sample(row))
            continue

        confidence_issues = _source_confidence_issues(event, source_ids)
        if confidence_issues:
            source_confidence_issue_counts.update(confidence_issues)
            source_confidence_diagnostic_samples.append(
                {**_sample(row), "issues": confidence_issues}
            )

        translation_join_ok = True
        for utterance_id in unique_source_ids:
            stt_row = stt_index.get((run_id, utterance_id))
            audio_path = audio_root / run_id / f"{utterance_id}.wav"
            chunk_ref = {
                **_sample(row),
                "utterance_id": utterance_id,
                "audio_path": str(audio_path.resolve(strict=False)),
            }
            if stt_row is None:
                missing_stt_events.append(chunk_ref)
                translation_join_ok = False
            else:
                stt_event = stt_row["event"]
                if stt_event.get("avg_logprob") is None or stt_event.get("no_speech_prob") is None:
                    missing_confidence.append(chunk_ref)
                if stt_event.get("audio_seconds") is None:
                    missing_audio_seconds.append(chunk_ref)
            if not audio_path.exists():
                missing_audio_files.append(chunk_ref)
                translation_join_ok = False
        if translation_join_ok:
            audio_join_ok_translations += 1

    total_translations = len(translations)
    return {
        "translation_events": total_translations,
        "translations_with_source_ids": total_translations - len(missing_source_id_samples),
        "missing_source_id_translations": len(missing_source_id_samples),
        "missing_source_id_samples": missing_source_id_samples[:top_n],
        "source_id_refs": source_ref_count,
        "unique_source_id_refs": unique_source_ref_count,
        "duplicate_source_id_refs": duplicate_source_ref_count,
        "translations_with_duplicate_source_ids": len(duplicate_source_id_samples),
        "duplicate_source_id_samples": duplicate_source_id_samples[:top_n],
        "multi_chunk_translations": multi_chunk_translations,
        "audio_join_ok_translations": audio_join_ok_translations,
        "audio_join_rate": _ratio(audio_join_ok_translations, total_translations),
        "missing_stt_event_refs": len(missing_stt_events),
        "missing_stt_event_samples": missing_stt_events[:top_n],
        "missing_audio_file_refs": len(missing_audio_files),
        "missing_audio_file_samples": missing_audio_files[:top_n],
        "missing_confidence_refs": len(missing_confidence),
        "missing_confidence_samples": missing_confidence[:top_n],
        "missing_audio_seconds_refs": len(missing_audio_seconds),
        "missing_audio_seconds_samples": missing_audio_seconds[:top_n],
        "source_confidence_diagnostic_issues": sum(source_confidence_issue_counts.values()),
        "source_confidence_issue_counts": [
            {"value": value, "count": count}
            for value, count in source_confidence_issue_counts.most_common()
        ],
        "source_confidence_diagnostic_samples": source_confidence_diagnostic_samples[:top_n],
    }


def _run_summaries(
    translations: list[dict[str, Any]],
    stt_rows: list[dict[str, Any]],
    audio_rows: list[dict[str, Any]],
    audio_root: Path,
) -> list[dict[str, Any]]:
    translation_groups = _group_by_run(translations)
    stt_groups = _group_by_run(stt_rows)
    audio_groups = _group_by_run(audio_rows)
    run_ids = sorted(set(translation_groups) | set(stt_groups) | set(audio_groups))
    summaries: list[dict[str, Any]] = []
    for run_id in run_ids:
        run_translations = translation_groups.get(run_id, [])
        run_dir = audio_root / run_id
        summaries.append(
            {
                "run_id": run_id,
                **_time_summary(run_translations),
                "translation_events": len(run_translations),
                "stt_events": len(stt_groups.get(run_id, [])),
                "audio_events": len(audio_groups.get(run_id, [])),
                "profiles": _counter(run_translations, "profile_id"),
                "status_breakdown": _counter(run_translations, "status"),
                "audio_dump_dir": str(run_dir.resolve(strict=False)),
                "audio_dump_dir_exists": run_dir.exists(),
                "wav_files": _count_wavs(run_dir),
            }
        )
    return summaries


def _suspicious_samples(
    translations: list[dict[str, Any]],
    top_n: int,
    long_text_chars: int,
) -> dict[str, Any]:
    empty_source = [
        _sample(row) for row in translations
        if not str(row["event"].get("source_text") or "").strip()
    ]
    empty_target = [
        _sample(row) for row in translations
        if not str(row["event"].get("target_text") or "").strip()
    ]
    very_short_source = [
        _sample(row) for row in translations
        if 0 < len(str(row["event"].get("source_text") or "").strip()) <= 2
    ]
    very_long_source = [
        {**_sample(row), "source_text_chars": len(str(row["event"].get("source_text") or ""))}
        for row in translations
        if len(str(row["event"].get("source_text") or "")) >= long_text_chars
    ]
    glossary_echo = [
        {**_sample(row), "reason": "numbered_repeated_list"}
        for row in translations
        if _looks_like_numbered_list_echo(str(row["event"].get("source_text") or ""))
    ]
    repeated_source_text = _repeated_source_text(translations, top_n)
    return {
        "empty_source_text": {
            "count": len(empty_source),
            "samples": empty_source[:top_n],
        },
        "empty_target_text": {
            "count": len(empty_target),
            "samples": empty_target[:top_n],
        },
        "very_short_source_text": {
            "count": len(very_short_source),
            "samples": very_short_source[:top_n],
        },
        "very_long_source_text": {
            "threshold_chars": long_text_chars,
            "count": len(very_long_source),
            "samples": very_long_source[:top_n],
        },
        "glossary_echo_candidates": {
            "count": len(glossary_echo),
            "samples": glossary_echo[:top_n],
        },
        "repeated_source_text": repeated_source_text,
    }


def _repeated_source_text(translations: list[dict[str, Any]], top_n: int) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {}
    display_text: dict[str, str] = {}
    for row in translations:
        text = str(row["event"].get("source_text") or "").strip()
        normalized = _normalize_text(text)
        if not normalized:
            continue
        groups.setdefault(normalized, []).append(row)
        display_text.setdefault(normalized, text)
    repeated = [
        {
            "source_text": _short(display_text[key], 160),
            "count": len(rows),
            "samples": [_sample(row) for row in rows[: min(top_n, 3)]],
        }
        for key, rows in groups.items()
        if len(rows) > 1
    ]
    repeated.sort(key=lambda item: item["count"], reverse=True)
    return {
        "unique_repeated_texts": len(repeated),
        "duplicate_event_count": sum(item["count"] - 1 for item in repeated),
        "samples": repeated[:top_n],
    }


def _looks_like_numbered_list_echo(text: str) -> bool:
    normalized = _normalize_text(text)
    if normalized.count(",") < 6:
        return False
    sections = re.findall(r"(?:^|[.!?]\s*)(\d+)\s*,\s*([^.!?]+)", normalized)
    if len(sections) < 2:
        return False
    token_sets = [
        set(_list_tokens(section_text))
        for _, section_text in sections[:3]
    ]
    if any(len(tokens) < 4 for tokens in token_sets[:2]):
        return False
    first = token_sets[0]
    for tokens in token_sets[1:]:
        overlap = len(first & tokens) / max(1, min(len(first), len(tokens)))
        if overlap >= 0.6:
            return True
    return False


def _list_tokens(text: str) -> list[str]:
    tokens = []
    for raw in text.split(","):
        token = re.sub(r"^\s*\d+\s*", "", raw).strip().casefold()
        token = token.strip(" .!?;:()[]{}")
        if token:
            tokens.append(token)
    return tokens


def _confidence_summary(stt_rows: list[dict[str, Any]]) -> dict[str, Any]:
    avg_logprobs = [
        value for row in stt_rows
        if (value := _float_or_none(row["event"].get("avg_logprob"))) is not None
    ]
    no_speech_probs = [
        value for row in stt_rows
        if (value := _float_or_none(row["event"].get("no_speech_prob"))) is not None
    ]
    audio_seconds = [
        value for row in stt_rows
        if (value := _float_or_none(row["event"].get("audio_seconds"))) is not None
    ]
    return {
        "stt_events": len(stt_rows),
        "avg_logprob": _numeric_summary(avg_logprobs),
        "no_speech_prob": _numeric_summary(no_speech_probs),
        "audio_seconds": _numeric_summary(audio_seconds),
        "missing_avg_logprob_events": len(stt_rows) - len(avg_logprobs),
        "missing_no_speech_prob_events": len(stt_rows) - len(no_speech_probs),
        "missing_audio_seconds_events": len(stt_rows) - len(audio_seconds),
    }


def _recommendations(
    *,
    translations: list[dict[str, Any]],
    min_population: int,
    evidence_failures: int,
    known_profile_count: int,
    unknown_profile_events: int,
    suspicious: dict[str, Any],
    run_ids: set[str] | None,
    run_summaries: list[dict[str, Any]],
) -> list[dict[str, str]]:
    recommendations: list[dict[str, str]] = []
    if len(translations) < min_population:
        recommendations.append(
            {
                "severity": "error",
                "message": f"Collect more schema_version==2 translation events: {len(translations)} < {min_population}.",
            }
        )
    if evidence_failures:
        recommendations.append(
            {
                "severity": "error",
                "message": "Fix source_utterance_ids/STT/wav/confidence join gaps before sampling.",
            }
        )
    if unknown_profile_events:
        recommendations.append(
            {
                "severity": "error",
                "message": "Missing profile_id values found; recollect or restrict to a clean schema_version==2 run.",
            }
        )
    if known_profile_count == 0 and translations:
        recommendations.append(
            {
                "severity": "error",
                "message": "No known profile_id found; cannot verify profile consistency.",
            }
        )
    if known_profile_count > 1:
        recommendations.append(
            {
                "severity": "error",
                "message": "Multiple profile_id values found; restrict with --run-id or recollect a clean run.",
            }
        )
    if run_ids is None and len(run_summaries) > 1:
        recommendations.append(
            {
                "severity": "warning",
                "message": "Multiple run_id values found; use --run-id for a single-run report when validating a fresh collection.",
            }
        )
    if suspicious["glossary_echo_candidates"]["count"]:
        recommendations.append(
            {
                "severity": "warning",
                "message": "Glossary/prompt echo candidates found; inspect these before sampling.",
            }
        )
    if suspicious["repeated_source_text"]["duplicate_event_count"]:
        recommendations.append(
            {
                "severity": "warning",
                "message": "Repeated source_text events found; check for duplicate STT or sentence merge issues.",
            }
        )
    if not recommendations:
        recommendations.append(
            {
                "severity": "ok",
                "message": "Collection is ready for unbiased sampling.",
            }
        )
    return recommendations


def _group_by_run(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["event"].get("run_id") or "unknown"), []).append(row)
    return grouped


def _counter(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    counts = Counter(str(row["event"].get(key) or "unknown") for row in rows)
    return [{"value": value, "count": count} for value, count in counts.most_common()]


def _time_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    timestamps = [
        parsed for row in rows
        if (parsed := _parse_datetime(row["event"].get("created_at"))) is not None
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


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in (str(item).strip() for item in value) if item]


def _source_confidence_issues(event: dict[str, Any], source_ids: list[str]) -> list[str]:
    issues: list[str] = []
    source_count = event.get("source_count")
    if source_count is None:
        issues.append("missing_source_count")
    else:
        try:
            if int(source_count) != len(source_ids):
                issues.append("source_count_mismatch")
        except (TypeError, ValueError):
            issues.append("source_count_not_integer")

    avg_values = event.get("source_avg_logprobs")
    no_speech_values = event.get("source_no_speech_probs")
    if not isinstance(avg_values, list):
        issues.append("missing_source_avg_logprobs")
        avg_values = []
    elif len(avg_values) != len(source_ids):
        issues.append("source_avg_logprobs_length_mismatch")

    if not isinstance(no_speech_values, list):
        issues.append("missing_source_no_speech_probs")
        no_speech_values = []
    elif len(no_speech_values) != len(source_ids):
        issues.append("source_no_speech_probs_length_mismatch")

    if "min_avg_logprob" not in event:
        issues.append("missing_min_avg_logprob")
    elif not _optional_float_equal(
        event.get("min_avg_logprob"),
        _aggregate_optional(avg_values[: len(source_ids)], min),
    ):
        issues.append("min_avg_logprob_mismatch")

    if "max_no_speech_prob" not in event:
        issues.append("missing_max_no_speech_prob")
    elif not _optional_float_equal(
        event.get("max_no_speech_prob"),
        _aggregate_optional(no_speech_values[: len(source_ids)], max),
    ):
        issues.append("max_no_speech_prob_mismatch")

    return issues


def _aggregate_optional(values: list[Any], reducer) -> float | None:
    numeric = [value for value in (_float_or_none(value) for value in values) if value is not None]
    return reducer(numeric) if numeric else None


def _optional_float_equal(actual: Any, expected: float | None) -> bool:
    if expected is None:
        return actual is None
    actual_float = _float_or_none(actual)
    return actual_float is not None and abs(actual_float - expected) <= 1e-9


def _unique_preserving_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique


def _sample(row: dict[str, Any]) -> dict[str, Any]:
    event = row["event"]
    return {
        "line_no": row["line_no"],
        "run_id": event.get("run_id", ""),
        "sequence_id": event.get("sequence_id"),
        "utterance_id": event.get("utterance_id", ""),
        "profile_id": event.get("profile_id", ""),
        "status": event.get("status", ""),
        "source_text": _short(str(event.get("source_text") or "")),
        "target_text": _short(str(event.get("target_text") or "")),
    }


def _short(text: str, limit: int = 120) -> str:
    text = text.replace("\r", " ").replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "..."


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _numeric_summary(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {"count": 0}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "avg": round(fmean(ordered), 4),
        "min": round(ordered[0], 4),
        "max": round(ordered[-1], 4),
    }


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _count_wavs(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for child in path.glob("*.wav") if child.is_file())


def _print_report(report: dict[str, Any]) -> None:
    if not report.get("available"):
        print(f"Collection sanity unavailable: {report['reason']} ({report['events_path']})")
        return

    counts = report["counts"]
    gate = report["sampling_gate"]
    join = report["join_quality"]
    print(f"Runtime events: {report['events_path']}")
    print(f"Audio root: {report['audio_root']}")
    print(
        f"Schema {report['schema_version']} translations: {counts['translation_events']} "
        f"(min {gate['min_population']}, passed={gate['passed']})"
    )
    print(f"Ready for sampling: {'yes' if report['ready_for_sampling'] else 'no'}")
    print(f"Profiles: {_format_counts(report['profiles'])}")
    print(f"Status: {_format_counts(report['status_breakdown'])}")
    print(
        "Audio/STT join: "
        f"{join['audio_join_ok_translations']}/{join['translation_events']} translations ok "
        f"(rate={join['audio_join_rate']})"
    )
    print(
        "Evidence gaps: "
        f"missing_source_ids={join['missing_source_id_translations']}, "
        f"missing_stt={join['missing_stt_event_refs']}, "
        f"missing_wav={join['missing_audio_file_refs']}, "
        f"missing_confidence={join['missing_confidence_refs']}, "
        f"source_diag_issues={join['source_confidence_diagnostic_issues']}"
    )
    print(
        "Chunk shape: "
        f"source_refs={join['source_id_refs']}, "
        f"unique_source_refs={join['unique_source_id_refs']}, "
        f"multi_chunk_translations={join['multi_chunk_translations']}, "
        f"duplicate_source_refs={join['duplicate_source_id_refs']}"
    )
    stt = report["stt_evidence"]
    print(
        "STT evidence: "
        f"avg_logprob={stt['avg_logprob']}, "
        f"no_speech_prob={stt['no_speech_prob']}, "
        f"audio_seconds={stt['audio_seconds']}"
    )
    print("\nRuns:")
    for run in report["run_summaries"]:
        print(
            f"- {run['run_id']}: translations={run['translation_events']}, "
            f"stt={run['stt_events']}, wav={run['wav_files']}, "
            f"profile={_format_counts(run['profiles'])}, "
            f"audio_dir={'yes' if run['audio_dump_dir_exists'] else 'no'}"
        )
    suspicious = report["suspicious"]
    print("\nSuspicious:")
    print(f"- empty_source_text: {suspicious['empty_source_text']['count']}")
    print(f"- empty_target_text: {suspicious['empty_target_text']['count']}")
    print(f"- very_short_source_text: {suspicious['very_short_source_text']['count']}")
    print(
        f"- very_long_source_text >= {suspicious['very_long_source_text']['threshold_chars']}: "
        f"{suspicious['very_long_source_text']['count']}"
    )
    print(f"- glossary_echo_candidates: {suspicious['glossary_echo_candidates']['count']}")
    print(f"- repeated_source_text duplicates: {suspicious['repeated_source_text']['duplicate_event_count']}")
    _print_sample_section("Glossary echo samples", suspicious["glossary_echo_candidates"]["samples"])
    _print_sample_section("Repeated source samples", suspicious["repeated_source_text"]["samples"])
    _print_sample_section("Missing audio samples", join["missing_audio_file_samples"])
    _print_sample_section("Missing STT samples", join["missing_stt_event_samples"])
    _print_sample_section("Source confidence diagnostic samples", join["source_confidence_diagnostic_samples"])
    print("\nRecommendations:")
    for item in report["recommendations"]:
        print(f"- {item['severity']}: {item['message']}")


def _print_sample_section(title: str, samples: list[dict[str, Any]]) -> None:
    if not samples:
        return
    print(f"\n{title}:")
    for sample in samples:
        if "samples" in sample:
            print(f"- count={sample['count']} source={sample['source_text']}")
            continue
        print(
            f"- line={sample.get('line_no')} seq={sample.get('sequence_id')} "
            f"run={sample.get('run_id')} source={sample.get('source_text')}"
        )


def _format_counts(items: list[dict[str, Any]]) -> str:
    return ", ".join(f"{item['value']}={item['count']}" for item in items) if items else "none"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate a collected runtime-events/audio-dump batch before labeling.")
    parser.add_argument("--events", type=Path, default=None, help="Path to runtime_events_YYYYMMDD.jsonl.")
    parser.add_argument("--audio-root", type=Path, default=DEFAULT_AUDIO_ROOT, help="Path to logs/audio_dump.")
    parser.add_argument(
        "--run-id",
        action="append",
        default=None,
        help="Restrict the report to this exact run_id. Repeat for multiple runs.",
    )
    parser.add_argument(
        "--min-population",
        type=int,
        default=DEFAULT_MIN_POPULATION,
        help="Minimum schema_version==2 translation population expected before sampling.",
    )
    parser.add_argument("--top", type=int, default=DEFAULT_TOP_N, help="Maximum samples shown per issue.")
    parser.add_argument(
        "--long-text-chars",
        type=int,
        default=DEFAULT_LONG_TEXT_CHARS,
        help="Source text length threshold for suspicious long sentence reporting.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--fail-on-not-ready", action="store_true", help="Exit 2 when ready_for_sampling is false.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    events_path = args.events or latest_event_file()
    if events_path is None:
        print("No runtime_events_*.jsonl file found.", file=sys.stderr)
        return 1
    report = build_collection_sanity_report(
        events_path=events_path,
        audio_root=args.audio_root,
        run_ids=set(args.run_id) if args.run_id else None,
        min_population=args.min_population,
        top_n=args.top,
        long_text_chars=args.long_text_chars,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_report(report)
    if args.fail_on_not_ready and not report.get("ready_for_sampling"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
