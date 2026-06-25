from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG_DIR = PROJECT_ROOT / "logs"
DEFAULT_AUDIO_ROOT = DEFAULT_LOG_DIR / "audio_dump"
TARGET_SCHEMA_VERSION = 2
DEFAULT_SAMPLE_SIZE = 50
DEFAULT_MIN_POPULATION = 150
DEFAULT_CONTEXT_TAG_OPTIONS = [
    "clip_audio",
    "bgm_mixed",
    "multi_speaker",
    "unclear_audio",
    "over_attributed_chunks",
]
DEFAULT_SPEAKER_SOURCE_OPTIONS = [
    "host_only",
    "clip_or_other_speaker",
    "host_over_clip",
    "multi_streamer",
    "speaker_unclear",
    "wrong_speaker_selected",
    "audio_source_mismatch",
]
MAX_PRIOR_TRANSLATION_REFERENCES = 3

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


def translation_population(
    rows: list[dict[str, Any]],
    run_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    return [
        row for row in rows
        if row["event"].get("schema_version") == TARGET_SCHEMA_VERSION
        and row["event"].get("event_type") == "translation"
        and (run_ids is None or str(row["event"].get("run_id") or "") in run_ids)
    ]


def _has_replayable_source_ids(row: dict[str, Any]) -> bool:
    event = row["event"]
    return bool(_string_list(event.get("source_utterance_ids")) or _string_list(
        event.get("evidence_source_utterance_ids")
    ))


def build_stt_index(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        event = row["event"]
        if event.get("schema_version") != TARGET_SCHEMA_VERSION:
            continue
        if event.get("event_type") != "stt":
            continue
        run_id = str(event.get("run_id") or "")
        utterance_id = str(event.get("utterance_id") or "")
        if run_id and utterance_id:
            index.setdefault((run_id, utterance_id), {"line_no": row["line_no"], "event": event})
    return index


def build_event_index(rows: list[dict[str, Any]], event_type: str) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        event = row["event"]
        if event.get("schema_version") != TARGET_SCHEMA_VERSION:
            continue
        if event.get("event_type") != event_type:
            continue
        run_id = str(event.get("run_id") or "")
        if run_id:
            index.setdefault(run_id, []).append({"line_no": row["line_no"], "event": event})
    for run_rows in index.values():
        run_rows.sort(key=lambda item: int(item["line_no"]))
    return index


def build_prior_source_usage(
    translation_rows: list[dict[str, Any]],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    usage: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in sorted(translation_rows, key=lambda item: int(item["line_no"])):
        event = row["event"]
        run_id = str(event.get("run_id") or "")
        if not run_id:
            continue
        record = {
            "translation_event_id": _translation_event_id(row),
            "translation_index": _translation_index(row),
            "line_no": row["line_no"],
            "sequence_id": event.get("sequence_id"),
            "utterance_id": str(event.get("utterance_id") or ""),
            "source_text": event.get("source_text", ""),
            "target_text": event.get("target_text", ""),
            "translation_cut_reason": _translation_cut_reason(event),
        }
        for utterance_id in _unique_preserving_order(_string_list(event.get("source_utterance_ids"))):
            usage.setdefault((run_id, utterance_id), []).append(record)
    return usage


def _translation_index(row: dict[str, Any]) -> int:
    sequence_id = row["event"].get("sequence_id")
    try:
        if sequence_id is not None:
            return int(sequence_id)
    except (TypeError, ValueError):
        pass
    return int(row["line_no"])


def _translation_event_id(row: dict[str, Any]) -> str:
    event = row["event"]
    stable_id = event.get("translation_event_id") or event.get("event_id")
    if stable_id:
        return str(stable_id)
    run_id = str(event.get("run_id") or "")
    return f"{run_id}:line:{row['line_no']}"


def _translation_cut_reason(event: dict[str, Any]) -> str:
    return str(event.get("cut_reason") or event.get("sentence_cut_reason") or "")


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in (str(item).strip() for item in value) if item]


def _unique_preserving_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique


def _audio_path(audio_root: Path, run_id: str, utterance_id: str) -> Path:
    return audio_root / run_id / f"{utterance_id}.wav"


def _source_role(
    *,
    utterance_id: str,
    primary_utterance_id: str,
    prior_translations: list[dict[str, Any]],
    legacy_prior_overlap: bool = True,
) -> tuple[str, str]:
    if utterance_id == primary_utterance_id:
        return "primary", "matches_translation_utterance_id"
    if legacy_prior_overlap and prior_translations:
        return "prior_overlap", "source_utterance_seen_in_prior_translation"
    return "supporting", "current_source_not_primary"


def _evidence_role(prior_translations: list[dict[str, Any]]) -> tuple[str, str]:
    if prior_translations:
        return "prior_overlap", "evidence_source_seen_in_prior_translation"
    return "evidence", "evidence_source_carry_forward"


def _nearest_prior_row(
    index: dict[str, list[dict[str, Any]]],
    *,
    run_id: str,
    line_no: int,
    source_utterance_ids: list[str] | None = None,
) -> dict[str, Any] | None:
    candidates = [row for row in index.get(run_id, []) if int(row["line_no"]) < line_no]
    if not candidates:
        return None
    if source_utterance_ids:
        source_set = set(source_utterance_ids)
        for row in reversed(candidates):
            row_source_ids = set(_string_list(row["event"].get("source_utterance_ids")))
            if source_set & row_source_ids:
                return row
    return candidates[-1]


def _audio_context(
    audio_index: dict[str, list[dict[str, Any]]],
    *,
    run_id: str,
    line_no: int | None,
) -> dict[str, Any] | None:
    if line_no is None:
        return None
    row = _nearest_prior_row(audio_index, run_id=run_id, line_no=line_no)
    if row is None:
        return None
    event = row["event"]
    return {
        "line_no": row["line_no"],
        "vad_cut_reason": event.get("cut_reason", ""),
        "audio_seconds": event.get("audio_seconds"),
        "raw_audio_seconds": event.get("raw_audio_seconds"),
        "overlap_seconds": event.get("overlap_seconds"),
        "adaptive_active": event.get("adaptive_active"),
    }


def _source_chunk(
    *,
    utterance_id: str,
    run_id: str,
    role: str,
    source_kind: str,
    prior_translations: list[dict[str, Any]],
    audio_root: Path,
    stt_index: dict[tuple[str, str], dict[str, Any]],
    audio_index: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    path = _audio_path(audio_root, run_id, utterance_id)
    stt_row = stt_index.get((run_id, utterance_id))
    stt_event = stt_row["event"] if stt_row else {}
    stt_line = stt_row["line_no"] if stt_row else None
    return {
        "utterance_id": utterance_id,
        "chunk_role": role,
        "source_kind": source_kind,
        "prior_translation_lines": [item["line_no"] for item in prior_translations],
        "prior_translation_sequence_ids": [item["sequence_id"] for item in prior_translations],
        "prior_translation_source_texts": [
            item["source_text"] for item in prior_translations[:MAX_PRIOR_TRANSLATION_REFERENCES]
        ],
        "audio_path": str(path.resolve(strict=False)),
        "audio_exists": path.exists(),
        "stt_event_found": stt_row is not None,
        "stt_event_line": stt_line,
        "stt_status": stt_event.get("status", ""),
        "stt_reason": stt_event.get("reason", ""),
        "stt_engine": stt_event.get("engine", ""),
        "stt_model": stt_event.get("model", ""),
        "stt_request_sent": stt_event.get("request_sent"),
        "stt_audio_seconds": stt_event.get("audio_seconds"),
        "stt_latency_ms": stt_event.get("latency_ms"),
        "stt_text_len": stt_event.get("text_len"),
        "avg_logprob": stt_event.get("avg_logprob"),
        "no_speech_prob": stt_event.get("no_speech_prob"),
        "audio_context": _audio_context(audio_index, run_id=run_id, line_no=stt_line),
    }


def _source_chunk_usage(
    *,
    utterance_id: str,
    run_id: str,
    role: str,
    role_reason: str,
    source_kind: str,
    prior_translations: list[dict[str, Any]],
    primary_utterance_id: str,
    current_translation_source_index: int | None,
    row: dict[str, Any],
    translation_cut_reason: str,
    evidence_source_index: int | None = None,
) -> dict[str, Any]:
    prior_indices = [item["translation_index"] for item in prior_translations]
    current_index = _translation_index(row)
    return {
        "utterance_id": utterance_id,
        "translation_event_id": _translation_event_id(row),
        "translation_index": current_index,
        "line_no": row["line_no"],
        "run_id": run_id,
        "translation_cut_reason": translation_cut_reason,
        "is_primary_for_this_translation": utterance_id == primary_utterance_id,
        "source_kind": source_kind,
        "role": role,
        "role_reason": role_reason,
        "first_seen_translation_index": prior_indices[0] if prior_indices else current_index,
        "prior_translation_indices": prior_indices,
        "prior_translation_event_ids": [item["translation_event_id"] for item in prior_translations],
        "prior_translation_lines": [item["line_no"] for item in prior_translations],
        "prior_translation_cut_reasons": [
            item["translation_cut_reason"] for item in prior_translations
        ],
        "current_translation_source_index": current_translation_source_index,
        "evidence_source_index": evidence_source_index,
    }


def _sample_entry(
    *,
    sample_index: int,
    row: dict[str, Any],
    audio_root: Path,
    stt_index: dict[tuple[str, str], dict[str, Any]],
    sentence_index: dict[str, list[dict[str, Any]]],
    audio_index: dict[str, list[dict[str, Any]]],
    prior_source_usage: dict[tuple[str, str], list[dict[str, Any]]],
) -> dict[str, Any]:
    event = row["event"]
    run_id = str(event.get("run_id") or "")
    primary_utterance_id = str(event.get("utterance_id") or "")
    source_utterance_ids = _string_list(event.get("source_utterance_ids"))
    evidence_source_utterance_ids = _string_list(event.get("evidence_source_utterance_ids"))
    unique_source_ids = _unique_preserving_order(source_utterance_ids)
    unique_evidence_source_ids = _unique_preserving_order(evidence_source_utterance_ids)
    has_explicit_evidence = "evidence_source_utterance_ids" in event
    nearest_source_ids = source_utterance_ids + evidence_source_utterance_ids
    nearest_sentence_row = _nearest_prior_row(
        sentence_index,
        run_id=run_id,
        line_no=int(row["line_no"]),
        source_utterance_ids=nearest_source_ids,
    )
    nearest_sentence_event = nearest_sentence_row["event"] if nearest_sentence_row else {}
    translation_cut_reason = _translation_cut_reason(event) or str(
        nearest_sentence_event.get("cut_reason") or ""
    )
    source_chunks = []
    for utterance_id in unique_source_ids:
        prior_translations = [
            item for item in prior_source_usage.get((run_id, utterance_id), [])
            if int(item["line_no"]) < int(row["line_no"])
        ]
        role, _role_reason = _source_role(
            utterance_id=utterance_id,
            primary_utterance_id=primary_utterance_id,
            prior_translations=prior_translations,
            legacy_prior_overlap=not has_explicit_evidence,
        )
        source_chunks.append(
            _source_chunk(
                utterance_id=utterance_id,
                run_id=run_id,
                role=role,
                source_kind="current",
                prior_translations=prior_translations,
                audio_root=audio_root,
                stt_index=stt_index,
                audio_index=audio_index,
            )
        )
    current_source_set = set(unique_source_ids)
    for utterance_id in unique_evidence_source_ids:
        if utterance_id in current_source_set:
            continue
        prior_translations = [
            item for item in prior_source_usage.get((run_id, utterance_id), [])
            if int(item["line_no"]) < int(row["line_no"])
        ]
        role, _role_reason = _evidence_role(prior_translations)
        source_chunks.append(
            _source_chunk(
                utterance_id=utterance_id,
                run_id=run_id,
                role=role,
                source_kind="evidence",
                prior_translations=prior_translations,
                audio_root=audio_root,
                stt_index=stt_index,
                audio_index=audio_index,
            )
        )
    source_chunk_usages = []
    for source_index, utterance_id in enumerate(source_utterance_ids):
        prior_translations = [
            item for item in prior_source_usage.get((run_id, utterance_id), [])
            if int(item["line_no"]) < int(row["line_no"])
        ]
        role, role_reason = _source_role(
            utterance_id=utterance_id,
            primary_utterance_id=primary_utterance_id,
            prior_translations=prior_translations,
            legacy_prior_overlap=not has_explicit_evidence,
        )
        source_chunk_usages.append(
            _source_chunk_usage(
                utterance_id=utterance_id,
                run_id=run_id,
                role=role,
                role_reason=role_reason,
                source_kind="current",
                prior_translations=prior_translations,
                primary_utterance_id=primary_utterance_id,
                current_translation_source_index=source_index,
                row=row,
                translation_cut_reason=translation_cut_reason,
            )
        )
    for evidence_index, utterance_id in enumerate(evidence_source_utterance_ids):
        prior_translations = [
            item for item in prior_source_usage.get((run_id, utterance_id), [])
            if int(item["line_no"]) < int(row["line_no"])
        ]
        role, role_reason = _evidence_role(prior_translations)
        source_chunk_usages.append(
            _source_chunk_usage(
                utterance_id=utterance_id,
                run_id=run_id,
                role=role,
                role_reason=role_reason,
                source_kind="evidence",
                prior_translations=prior_translations,
                primary_utterance_id=primary_utterance_id,
                current_translation_source_index=None,
                evidence_source_index=evidence_index,
                row=row,
                translation_cut_reason=translation_cut_reason,
            )
        )
    role_counts = {
        "primary": sum(1 for chunk in source_chunks if chunk["chunk_role"] == "primary"),
        "prior_overlap": sum(1 for chunk in source_chunks if chunk["chunk_role"] == "prior_overlap"),
        "supporting": sum(1 for chunk in source_chunks if chunk["chunk_role"] == "supporting"),
    }
    evidence_role_count = sum(1 for chunk in source_chunks if chunk["chunk_role"] == "evidence")
    if evidence_role_count:
        role_counts["evidence"] = evidence_role_count
    prior_overlap_utterance_ids = [
        chunk["utterance_id"] for chunk in source_chunks
        if chunk["chunk_role"] == "prior_overlap" or chunk["source_kind"] == "evidence"
    ]
    return {
        "sample_id": f"S{sample_index:03d}",
        "translation_event_id": _translation_event_id(row),
        "translation_index": _translation_index(row),
        "runtime_event_line": row["line_no"],
        "run_id": run_id,
        "created_at": event.get("created_at", ""),
        "sequence_id": event.get("sequence_id"),
        "utterance_id": primary_utterance_id,
        "primary_source_utterance_id": primary_utterance_id,
        "source_utterance_ids": source_utterance_ids,
        "unique_source_utterance_ids": unique_source_ids,
        "source_utterance_id_count": len(source_utterance_ids),
        "unique_source_utterance_id_count": len(unique_source_ids),
        "evidence_source_utterance_ids": evidence_source_utterance_ids,
        "unique_evidence_source_utterance_ids": unique_evidence_source_ids,
        "evidence_source_utterance_id_count": len(evidence_source_utterance_ids),
        "unique_evidence_source_utterance_id_count": len(unique_evidence_source_ids),
        "source_chunks": source_chunks,
        "source_chunk_usages": source_chunk_usages,
        "source_overlap_warning": bool(prior_overlap_utterance_ids),
        "prior_overlap_utterance_ids": prior_overlap_utterance_ids,
        "chunk_role_counts": role_counts,
        "translation_cut_reason": translation_cut_reason,
        "nearest_sentence_event_line": nearest_sentence_row["line_no"] if nearest_sentence_row else None,
        "sentence_cut_reason": nearest_sentence_event.get("cut_reason", ""),
        "sentence_forced": nearest_sentence_event.get("forced"),
        "sentence_source_utterance_ids": _string_list(
            nearest_sentence_event.get("source_utterance_ids")
        ),
        "sentence_evidence_source_utterance_ids": _string_list(
            nearest_sentence_event.get("evidence_source_utterance_ids")
        ),
        "sentence_chunk_count": nearest_sentence_event.get("chunk_count"),
        "sentence_audio_seconds": nearest_sentence_event.get("audio_seconds"),
        "source_text": event.get("source_text", ""),
        "target_text": event.get("target_text", ""),
        "status": event.get("status", ""),
        "filter_reason": event.get("filter_reason", ""),
        "engine": event.get("engine", ""),
        "model": event.get("model", ""),
        "prompt_version": event.get("prompt_version", ""),
        "profile_id": event.get("profile_id", ""),
        "profile_applied": event.get("profile_applied"),
        "quality_score": event.get("quality_score"),
        "quality_flags": event.get("quality_flags", []),
        "annotation": {
            "label": "",
            "context_tags": [],
            "heard_source_text": "",
            "notes": "",
        },
    }


def build_labeling_sample(
    *,
    events_path: Path,
    audio_root: Path = DEFAULT_AUDIO_ROOT,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    seed: int | None = None,
    min_population: int = DEFAULT_MIN_POPULATION,
    allow_missing_audio: bool = False,
    run_ids: set[str] | None = None,
) -> dict[str, Any]:
    if sample_size < 1:
        raise ValueError("sample_size must be positive")
    if min_population < 0:
        raise ValueError("min_population must be non-negative")

    rows = read_runtime_rows(events_path)
    raw_population = translation_population(rows, run_ids)
    excluded_missing_source_id_population = 0
    if allow_missing_audio:
        population = raw_population
    else:
        population = [row for row in raw_population if _has_replayable_source_ids(row)]
        excluded_missing_source_id_population = len(raw_population) - len(population)
    if len(population) < min_population:
        raise ValueError(
            f"population too small: {len(population)} < {min_population}; "
            "collect more schema_version==2 replayable translation events, lower --min-population, "
            "or pass --allow-missing-audio"
        )
    if len(population) < sample_size:
        raise ValueError(f"sample_size {sample_size} exceeds population {len(population)}")

    selected_seed = seed
    if selected_seed is None:
        selected_seed = random.SystemRandom().randrange(0, 2**63)
    rng = random.Random(selected_seed)
    selected = rng.sample(population, sample_size)
    stt_index = build_stt_index(rows)
    sentence_index = build_event_index(rows, "sentence")
    audio_index = build_event_index(rows, "audio")
    prior_source_usage = build_prior_source_usage(population)
    samples = [
        _sample_entry(
            sample_index=index,
            row=row,
            audio_root=audio_root,
            stt_index=stt_index,
            sentence_index=sentence_index,
            audio_index=audio_index,
            prior_source_usage=prior_source_usage,
        )
        for index, row in enumerate(selected, start=1)
    ]

    missing_source_id_samples = [
        sample["sample_id"] for sample in samples
        if not sample["source_utterance_ids"] and not sample["evidence_source_utterance_ids"]
    ]
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
    if not allow_missing_audio and (missing_source_id_samples or missing_audio or missing_stt_events):
        raise ValueError(
            "sample contains entries without replayable audio evidence: "
            f"missing_source_id_samples={len(missing_source_id_samples)}, "
            f"missing_audio_files={len(missing_audio)}, "
            f"missing_stt_events={len(missing_stt_events)}"
        )

    return {
        "labeling_sample_schema": 1,
        "speaker_policy": "host-primary",
        "annotation_goal": (
            "Classify each translation under host-primary policy: host/main speaker has "
            "priority; when host is silent, clear clip/game/other speech can be valid source."
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
            "Do not estimate rates from hand-picked or quality-filtered samples.",
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
            "method": "uniform_random_without_replacement",
            "events_path": str(events_path.resolve(strict=False)),
            "audio_root": str(audio_root.resolve(strict=False)),
            "schema_version": TARGET_SCHEMA_VERSION,
            "event_type": "translation",
            "run_ids": sorted(run_ids) if run_ids is not None else [],
            "seed": selected_seed,
            "raw_population_size": len(raw_population),
            "excluded_missing_source_id_population": excluded_missing_source_id_population,
            "population_size": len(population),
            "sample_size": sample_size,
            "min_population": min_population,
        },
        "quality_control": {
            "missing_source_id_samples": missing_source_id_samples,
            "missing_audio_files": missing_audio,
            "missing_stt_events": missing_stt_events,
        },
        "samples": samples,
    }


def default_output_path() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return DEFAULT_LOG_DIR / f"labeling_sample_{stamp}.json"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Uniformly sample schema_version==2 translation events for human STT-vs-translation labeling."
    )
    parser.add_argument("--events", type=Path, default=None, help="Path to runtime_events_YYYYMMDD.jsonl.")
    parser.add_argument("--audio-root", type=Path, default=DEFAULT_AUDIO_ROOT, help="Path to logs/audio_dump.")
    parser.add_argument("--output", type=Path, default=None, help="Output JSON path.")
    parser.add_argument(
        "--run-id",
        action="append",
        default=None,
        help="Restrict the sampling population to this exact run_id. Repeat for multiple runs.",
    )
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLE_SIZE, help="Number of translation events to sample.")
    parser.add_argument("--seed", type=int, default=None, help="Random seed for reproducible sampling.")
    parser.add_argument(
        "--min-population",
        type=int,
        default=DEFAULT_MIN_POPULATION,
        help="Minimum schema_version==2 translation-event population required before sampling.",
    )
    parser.add_argument(
        "--allow-missing-audio",
        action="store_true",
        help="Write the sample even if sampled events have missing source ids, wav files, or STT confidence events.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    events_path = args.events or latest_event_file()
    if events_path is None:
        print("No runtime_events_*.jsonl file found.", file=sys.stderr)
        return 1

    output_path = args.output or default_output_path()
    try:
        data = build_labeling_sample(
            events_path=events_path,
            audio_root=args.audio_root,
            sample_size=args.samples,
            seed=args.seed,
            min_population=args.min_population,
            allow_missing_audio=args.allow_missing_audio,
            run_ids=set(args.run_id) if args.run_id else None,
        )
    except ValueError as exc:
        print(f"Sampling failed: {exc}", file=sys.stderr)
        return 2

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    missing_audio_count = len(data["quality_control"]["missing_audio_files"])
    missing_source_count = len(data["quality_control"]["missing_source_id_samples"])
    print(
        "Wrote {samples} samples from population {population} to {path} "
        "(seed={seed}, missing_source_ids={missing_source}, missing_audio={missing_audio})".format(
            samples=data["sampling"]["sample_size"],
            population=data["sampling"]["population_size"],
            path=output_path,
            seed=data["sampling"]["seed"],
            missing_source=missing_source_count,
            missing_audio=missing_audio_count,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
