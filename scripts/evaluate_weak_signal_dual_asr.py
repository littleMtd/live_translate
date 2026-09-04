from __future__ import annotations

import argparse
import collections
import difflib
import hashlib
import json
import re
import statistics
import sys
import unicodedata
from pathlib import Path
from typing import Any, Iterable

import soundfile as sf


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVENTS = PROJECT_ROOT / "logs" / "runtime_events_20260802.jsonl"
DEFAULT_PROVENANCE = PROJECT_ROOT / "data" / "semantic_quality_evidence_20260802.json"
DEFAULT_MANIFEST = PROJECT_ROOT / "scratch" / "analysis" / "t25_weak_signal_manifest_20260803.json"
DEFAULT_ANALYSIS = PROJECT_ROOT / "scratch" / "analysis" / "t25_weak_signal_dual_asr_20260803.json"

WEAK_LOGPROB_MIN = -1.0
WEAK_LOGPROB_MAX = -0.7
CONTROL_LOGPROB_MIN = -0.3
RAW_RMS_MAX = 0.01
NO_SPEECH_MAX = 0.3
DURATION_CALIPER_SECONDS = 1.5
RMS_CALIPER = 0.006
LOCAL_AGREEMENT_MIN = 0.8
LOCAL_TO_GROQ_MAX = 0.5


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object: {path}")
    return data


def _events(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                event = dict(event)
                event["_line_number"] = line_number
                rows.append(event)
    return rows


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _display_path(path: Path, project_root: Path) -> str:
    try:
        return str(path.resolve().relative_to(project_root.resolve()))
    except ValueError:
        return str(path.resolve())


def _utterance_number(value: Any) -> int:
    match = re.search(r"(\d+)$", str(value or ""))
    return int(match.group(1)) if match else sys.maxsize


def _effective_runs(provenance: dict[str, Any]) -> set[str]:
    summaries = provenance.get("run_summaries")
    if not isinstance(summaries, list) or not summaries:
        raise ValueError("provenance has no run_summaries")
    runs = {
        str(row.get("run_id") or "")
        for row in summaries
        if isinstance(row, dict) and str(row.get("run_id") or "")
    }
    if not runs:
        raise ValueError("provenance run_summaries contain no run ids")
    return runs


def _annotated_audio_keys(provenance: dict[str, Any]) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    annotations = provenance.get("annotations")
    if not isinstance(annotations, list):
        raise ValueError("provenance has no annotations list")
    for annotation in annotations:
        if not isinstance(annotation, dict):
            continue
        for match in annotation.get("timestamp_matches") or []:
            if not isinstance(match, dict):
                continue
            for runtime_ref in match.get("runtime_refs") or []:
                if not isinstance(runtime_ref, dict):
                    continue
                run_id = str(runtime_ref.get("run_id") or "")
                for audio_ref in runtime_ref.get("audio_refs") or []:
                    if isinstance(audio_ref, dict):
                        utterance_id = str(audio_ref.get("utterance_id") or "")
                        if run_id and utterance_id:
                            keys.add((run_id, utterance_id))
    return keys


def _translation_alignment(
    events: Iterable[dict[str, Any]],
) -> dict[tuple[str, str], tuple[str, str]]:
    """Return comparison-safe Groq text or a structural exclusion reason."""
    related: dict[tuple[str, str], list[dict[str, Any]]] = collections.defaultdict(list)
    stt_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for event in events:
        run_id = str(event.get("run_id") or "")
        if event.get("event_type") == "stt":
            stt_by_key[(run_id, str(event.get("utterance_id") or ""))] = event
        utterance_ids = event.get("source_utterance_ids")
        if (
            event.get("event_type") == "translation"
            and event.get("status") == "success"
            and isinstance(utterance_ids, list)
        ):
            for utterance_id in utterance_ids:
                related[(run_id, str(utterance_id or ""))].append(event)

    output: dict[tuple[str, str], tuple[str, str]] = {}
    for key, stt in stt_by_key.items():
        rows = related.get(key, [])
        eligible = next(
            (
                row
                for row in rows
                if row.get("source_utterance_ids") == [key[1]]
                and int(row.get("source_count") or 1) == 1
                and isinstance(row.get("evidence_source_utterance_ids"), list)
                and not row["evidence_source_utterance_ids"]
                and str(row.get("source_text") or "").strip()
            ),
            None,
        )
        if eligible is None:
            reason = "translation_unavailable"
            if any(
                isinstance(row.get("evidence_source_utterance_ids"), list)
                and row.get("evidence_source_utterance_ids")
                for row in rows
            ):
                reason = "evidence_bearing_translation"
            elif rows:
                reason = "multi_source_or_ineligible_translation"
            output[key] = ("", reason)
            continue
        text = str(eligible.get("source_text") or "")
        text_len = stt.get("text_len")
        if isinstance(text_len, int) and len(text) == text_len:
            output[key] = (text, "single_source_length_match")
        else:
            output[key] = ("", "text_length_mismatch")
    return output


def _audio_row(
    event: dict[str, Any],
    *,
    audio_root: Path,
    project_root: Path,
    alignment: dict[tuple[str, str], tuple[str, str]],
) -> dict[str, Any] | None:
    run_id = str(event.get("run_id") or "")
    utterance_id = str(event.get("utterance_id") or "")
    audio_path = audio_root / run_id / f"{utterance_id}.wav"
    if not run_id or not utterance_id or not audio_path.is_file():
        return None
    info = sf.info(audio_path)
    if info.samplerate != 16000 or info.channels != 1 or info.subtype != "PCM_16":
        raise ValueError(f"unexpected retained WAV format: {audio_path}")
    telemetry_seconds = event.get("audio_seconds")
    actual_seconds = info.frames / info.samplerate
    if not isinstance(telemetry_seconds, (int, float)) or abs(actual_seconds - telemetry_seconds) > 0.001:
        raise ValueError(f"WAV duration does not match telemetry: {audio_path}")
    groq_text, comparison_reason = alignment.get(
        (run_id, utterance_id), ("", "translation_unavailable")
    )
    return {
        "run_id": run_id,
        "utterance_id": utterance_id,
        "created_at": str(event.get("created_at") or ""),
        "profile_id": str(event.get("profile_id") or ""),
        "engine": str(event.get("engine") or ""),
        "model": str(event.get("model") or ""),
        "avg_logprob": event.get("avg_logprob"),
        "no_speech_prob": event.get("no_speech_prob"),
        "audio_rms": event.get("audio_rms"),
        "audio_seconds": round(actual_seconds, 3),
        "overlap_seconds": float(event.get("overlap_seconds") or 0.0),
        "vad_cut_reason": str(event.get("vad_cut_reason") or ""),
        "context_included": event.get("context_included") is True,
        "runtime_event_line": event.get("_line_number"),
        "audio_path": _display_path(audio_path, project_root),
        "size_bytes": audio_path.stat().st_size,
        "sha256": _sha256(audio_path),
        "groq_text": groq_text,
        "groq_comparison_eligible": bool(groq_text),
        "groq_comparison_reason": comparison_reason,
    }


def _eligible_base(row: dict[str, Any]) -> bool:
    return (
        row["engine"] == "groq"
        and row["model"] == "whisper-large-v3"
        and row["context_included"]
        and isinstance(row.get("no_speech_prob"), (int, float))
        and float(row["no_speech_prob"]) <= NO_SPEECH_MAX
        and isinstance(row.get("audio_rms"), (int, float))
        and float(row["audio_rms"]) < RAW_RMS_MAX
        and isinstance(row.get("avg_logprob"), (int, float))
    )


def _match_controls(
    weak_rows: list[dict[str, Any]], control_pool: list[dict[str, Any]]
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    used: set[tuple[str, str]] = set()
    pairs = []
    for weak in sorted(
        weak_rows, key=lambda row: (row["run_id"], _utterance_number(row["utterance_id"]))
    ):
        candidates = []
        for control in control_pool:
            key = control["run_id"], control["utterance_id"]
            if key in used:
                continue
            if any(
                (
                    control[field] != weak[field]
                    for field in ("run_id", "profile_id", "model", "vad_cut_reason")
                )
            ):
                continue
            if (control["overlap_seconds"] > 0) != (weak["overlap_seconds"] > 0):
                continue
            if control["groq_comparison_eligible"] != weak["groq_comparison_eligible"]:
                continue
            duration_delta = abs(control["audio_seconds"] - weak["audio_seconds"])
            rms_delta = abs(float(control["audio_rms"]) - float(weak["audio_rms"]))
            if duration_delta > DURATION_CALIPER_SECONDS or rms_delta > RMS_CALIPER:
                continue
            candidates.append((duration_delta, rms_delta, control["created_at"], _utterance_number(control["utterance_id"]), control))
        if not candidates:
            raise ValueError(
                "no matched control within frozen calipers for "
                f"{weak['run_id']}:{weak['utterance_id']}"
            )
        control = min(candidates, key=lambda item: item[:4])[4]
        used.add((control["run_id"], control["utterance_id"]))
        pairs.append((weak, control))
    return pairs


def _case(row: dict[str, Any], *, cohort: str, pair_id: str) -> dict[str, Any]:
    sample_id = f"T25-WS-{pair_id}-{cohort}"
    chunk = {
        "utterance_id": row["utterance_id"],
        "chunk_role": "single_stt_chunk",
        "source_kind": "current",
        "stt_status": "success",
        "stt_reason": "",
        "stt_engine": row["engine"],
        "stt_model": row["model"],
        "stt_audio_seconds": row["audio_seconds"],
        "avg_logprob": row["avg_logprob"],
        "no_speech_prob": row["no_speech_prob"],
        "stt_event_line": row["runtime_event_line"],
        "audio_context": {
            "vad_cut_reason": row["vad_cut_reason"],
            "audio_rms": row["audio_rms"],
            "overlap_seconds": row["overlap_seconds"],
            "context_included": row["context_included"],
        },
    }
    return {
        "sample_id": sample_id,
        "root_cause_group": cohort,
        "ground_truth_status": "unlabeled_candidate",
        "pair_id": pair_id,
        "cohort": cohort,
        "groq_comparison_eligible": row["groq_comparison_eligible"],
        "groq_comparison_reason": row["groq_comparison_reason"],
        "annotation": None,
        "audio_assets": [
            {
                "utterance_id": row["utterance_id"],
                "chunk_role": "single_stt_chunk",
                "source_kind": "current",
                "audio_path": row["audio_path"],
                "size_bytes": row["size_bytes"],
                "sha256": row["sha256"],
                "audio_seconds": row["audio_seconds"],
            }
        ],
        "sample": {
            "run_id": row["run_id"],
            "profile_ids": [row["profile_id"]],
            "source_text": row["groq_text"],
            "source_utterance_ids": [row["utterance_id"]],
            "evidence_source_utterance_ids": [],
            "source_chunks": [chunk],
        },
    }


def build_manifest(
    *,
    events_path: Path,
    provenance_path: Path,
    audio_root: Path,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    provenance = _read_json(provenance_path)
    effective_runs = _effective_runs(provenance)
    annotated_keys = _annotated_audio_keys(provenance)
    events = _events(events_path)
    alignment = _translation_alignment(events)
    population: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for event in events:
        if (
            event.get("event_type") != "stt"
            or event.get("status") != "success"
            or event.get("request_sent") is False
            or event.get("run_kind") != "live"
            or str(event.get("run_id") or "") not in effective_runs
        ):
            continue
        key = str(event.get("run_id") or ""), str(event.get("utterance_id") or "")
        if key in seen:
            raise ValueError(f"duplicate successful STT key: {key}")
        seen.add(key)
        row = _audio_row(
            event,
            audio_root=audio_root,
            project_root=project_root,
            alignment=alignment,
        )
        if row is not None:
            population.append(row)

    weak = [
        row
        for row in population
        if _eligible_base(row)
        and WEAK_LOGPROB_MIN <= float(row["avg_logprob"]) < WEAK_LOGPROB_MAX
    ]
    controls = [
        row
        for row in population
        if _eligible_base(row)
        and float(row["avg_logprob"]) >= CONTROL_LOGPROB_MIN
        and (row["run_id"], row["utterance_id"]) not in annotated_keys
    ]
    if not weak:
        raise ValueError("frozen weak-signal selection produced no cases")
    pairs = _match_controls(weak, controls)
    cases = []
    match_rows = []
    for index, (weak_row, control_row) in enumerate(pairs, 1):
        pair_id = f"pair-{index:03d}"
        cases.extend(
            (
                _case(weak_row, cohort="weak_signal", pair_id=pair_id),
                _case(control_row, cohort="matched_control", pair_id=pair_id),
            )
        )
        match_rows.append(
            {
                "pair_id": pair_id,
                "weak_key": [weak_row["run_id"], weak_row["utterance_id"]],
                "control_key": [control_row["run_id"], control_row["utterance_id"]],
                "duration_delta_seconds": round(abs(weak_row["audio_seconds"] - control_row["audio_seconds"]), 3),
                "raw_rms_delta": round(abs(float(weak_row["audio_rms"]) - float(control_row["audio_rms"])), 6),
                "groq_comparison_eligible": weak_row["groq_comparison_eligible"],
            }
        )
    return {
        "phase0_replay_manifest_schema": 2,
        "experiment_schema": 1,
        "batch_id": "T25-weak-signal-dual-asr-20260803",
        "speaker_policy": "observed-mixed-audio; no speaker ground truth asserted",
        "runtime_events": _display_path(events_path, project_root),
        "source_provenance": _display_path(provenance_path, project_root),
        "effective_runs": sorted(effective_runs),
        "population_count": len(population),
        "weak_count": len(pairs),
        "control_count": len(pairs),
        "case_count": len(cases),
        "root_cause_group_counts": {"matched_control": len(pairs), "weak_signal": len(pairs)},
        "selection_contract": {
            "weak_avg_logprob": f"{WEAK_LOGPROB_MIN} <= x < {WEAK_LOGPROB_MAX}",
            "control_avg_logprob": f"x >= {CONTROL_LOGPROB_MIN}",
            "raw_audio_rms": f"x < {RAW_RMS_MAX}",
            "no_speech_prob": f"x <= {NO_SPEECH_MAX}",
            "context_included": True,
            "control_excludes_annotated_audio": True,
            "exact_match_fields": [
                "run_id",
                "profile_id",
                "model",
                "vad_cut_reason",
                "overlap_zero_or_positive",
                "groq_comparison_eligible",
            ],
            "duration_caliper_seconds": DURATION_CALIPER_SECONDS,
            "raw_rms_caliper": RMS_CALIPER,
            "matching": "one-to-one without replacement; duration delta, RMS delta, created_at, utterance number",
        },
        "interpretation_limit": (
            "Matched controls are unreviewed candidates, not known-correct transcripts. "
            "ASR agreement is a prioritization signal and cannot authorize live changes."
        ),
        "matches": match_rows,
        "cases": cases,
    }


def _normalized(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return "".join(char for char in normalized if char.isalnum())


def _similarity(left: str, right: str) -> float | None:
    left_norm = _normalized(left)
    right_norm = _normalized(right)
    if not left_norm or not right_norm:
        return None
    return round(difflib.SequenceMatcher(None, left_norm, right_norm).ratio(), 3)


def _output_proxy_flags(text: str) -> list[str]:
    """Return precision-oriented triage proxies, never correctness labels."""
    compact = _normalized(text)
    if not compact:
        return ["empty_output"]
    flags = []
    if len(compact) >= 12:
        distinct_bigram_ratio = len(set(zip(compact, compact[1:]))) / (len(compact) - 1)
        if distinct_bigram_ratio <= 0.2:
            flags.append("low_distinct_bigram_repetition")
    return flags


def analyze_dual_replay(
    *,
    manifest_path: Path,
    sensevoice_path: Path,
    faster_whisper_path: Path,
) -> dict[str, Any]:
    manifest = _read_json(manifest_path)
    sensevoice = _read_json(sensevoice_path)
    faster = _read_json(faster_whisper_path)
    manifest_sha = _sha256(manifest_path)
    for name, payload, expected_engine in (
        ("sensevoice", sensevoice, "sensevoice"),
        ("faster_whisper", faster, "faster_whisper"),
    ):
        if payload.get("manifest_sha256") != manifest_sha:
            raise ValueError(f"{name} result manifest SHA does not match")
        if payload.get("engine") != expected_engine:
            raise ValueError(f"unexpected {name} engine identity")
    case_defs = {str(row["sample_id"]): row for row in manifest.get("cases") or []}
    outputs = {}
    for engine, payload in (("sensevoice", sensevoice), ("faster_whisper", faster)):
        rows = payload.get("cases")
        if not isinstance(rows, list):
            raise ValueError(f"{engine} result has no cases list")
        mapped = {str(row.get("sample_id") or ""): row for row in rows if isinstance(row, dict)}
        if set(mapped) != set(case_defs):
            raise ValueError(f"{engine} result case ids do not match manifest")
        outputs[engine] = mapped

    rows = []
    for sample_id, case in case_defs.items():
        sv_text = str(outputs["sensevoice"][sample_id].get("candidate_current_text") or "")
        fw_text = str(outputs["faster_whisper"][sample_id].get("candidate_current_text") or "")
        groq_text = str((case.get("sample") or {}).get("source_text") or "")
        local_similarity = _similarity(sv_text, fw_text)
        sv_groq = _similarity(sv_text, groq_text)
        fw_groq = _similarity(fw_text, groq_text)
        comparable = all(value is not None for value in (local_similarity, sv_groq, fw_groq))
        consensus_disagreement = bool(
            comparable
            and local_similarity >= LOCAL_AGREEMENT_MIN
            and sv_groq <= LOCAL_TO_GROQ_MAX
            and fw_groq <= LOCAL_TO_GROQ_MAX
        )
        sensevoice_proxy_flags = _output_proxy_flags(sv_text)
        faster_proxy_flags = _output_proxy_flags(fw_text)
        rows.append(
            {
                "sample_id": sample_id,
                "pair_id": case.get("pair_id"),
                "cohort": case.get("cohort"),
                "run_id": (case.get("sample") or {}).get("run_id"),
                "utterance_id": ((case.get("sample") or {}).get("source_utterance_ids") or [""])[0],
                "groq_comparison_eligible": case.get("groq_comparison_eligible"),
                "groq_text": groq_text,
                "sensevoice_text": sv_text,
                "faster_whisper_text": fw_text,
                "local_similarity": local_similarity,
                "sensevoice_to_groq_similarity": sv_groq,
                "faster_whisper_to_groq_similarity": fw_groq,
                "three_way_comparable": comparable,
                "local_consensus_disagreement": consensus_disagreement,
                "sensevoice_proxy_flags": sensevoice_proxy_flags,
                "faster_whisper_proxy_flags": faster_proxy_flags,
            }
        )

    summaries = {}
    for cohort in ("weak_signal", "matched_control"):
        group = [row for row in rows if row["cohort"] == cohort]
        comparable = [row for row in group if row["three_way_comparable"]]
        local_similarities = [
            row["local_similarity"]
            for row in group
            if row["local_similarity"] is not None
        ]
        summaries[cohort] = {
            "case_count": len(group),
            "nonempty_both_local_count": sum(
                bool(_normalized(row["sensevoice_text"])) and bool(_normalized(row["faster_whisper_text"]))
                for row in group
            ),
            "three_way_comparable_count": len(comparable),
            "local_consensus_disagreement_count": sum(
                row["local_consensus_disagreement"] for row in comparable
            ),
            "local_consensus_disagreement_denominator": len(comparable),
            "local_similarity_median": (
                round(statistics.median(local_similarities), 3)
                if local_similarities
                else None
            ),
            "local_exact_normalized_match_count": sum(
                bool(_normalized(row["sensevoice_text"]))
                and _normalized(row["sensevoice_text"])
                == _normalized(row["faster_whisper_text"])
                for row in group
            ),
        }
    engine_performance = {}
    for name, payload in (("sensevoice", sensevoice), ("faster_whisper", faster)):
        chunks = [
            chunk
            for case in payload.get("cases") or []
            if isinstance(case, dict)
            for chunk in case.get("candidate_chunks") or []
            if isinstance(chunk, dict)
        ]
        latencies = [
            float(chunk["latency_ms"])
            for chunk in chunks
            if isinstance(chunk.get("latency_ms"), (int, float))
        ]
        audio_seconds = sum(
            float(chunk["audio_seconds"])
            for chunk in chunks
            if isinstance(chunk.get("audio_seconds"), (int, float))
        )
        latency_seconds = sum(latencies) / 1000.0
        result_path = sensevoice_path if name == "sensevoice" else faster_whisper_path
        engine_performance[name] = {
            "result_sha256": _sha256(result_path),
            "runtime_versions": payload.get("runtime_versions"),
            "chunk_count": len(chunks),
            "audio_seconds": round(audio_seconds, 3),
            "inference_seconds": round(latency_seconds, 3),
            "real_time_factor": round(latency_seconds / audio_seconds, 3) if audio_seconds else None,
            "latency_ms_median": round(statistics.median(latencies), 1) if latencies else None,
        }
    review = sorted(
        (row for row in rows if row["cohort"] == "weak_signal"),
        key=lambda row: (
            not bool(row["sensevoice_proxy_flags"] or row["faster_whisper_proxy_flags"]),
            not row["local_consensus_disagreement"],
            -(row["local_similarity"] if row["local_similarity"] is not None else -1.0),
            row["sample_id"],
        ),
    )
    return {
        "weak_signal_dual_asr_analysis_schema": 1,
        "manifest": str(manifest_path),
        "manifest_sha256": manifest_sha,
        "sensevoice_result": str(sensevoice_path),
        "faster_whisper_result": str(faster_whisper_path),
        "normalization": "Unicode NFKC + casefold + Unicode alphanumeric characters only",
        "local_consensus_disagreement_definition": {
            "local_similarity_min": LOCAL_AGREEMENT_MIN,
            "each_local_to_groq_similarity_max": LOCAL_TO_GROQ_MAX,
            "empty_text": "not comparable and excluded from denominator",
            "denominator": "three-way-comparable cases in the named cohort",
        },
        "summaries": summaries,
        "engine_performance": engine_performance,
        "live_shadow_decision": "no-go",
        "decision_reason": (
            "The fixed sample is hypothesis-generating and controls are not known-correct. "
            "Rates prioritize optional blinded listening; they do not measure rescue or false correction."
        ),
        "review_priority": [row["sample_id"] for row in review],
        "cases": rows,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build and evaluate the bounded T25 weak-signal dual-ASR cohort."
    )
    parser.add_argument("--mode", choices=("build", "analyze"), required=True)
    parser.add_argument("--events", type=Path, default=DEFAULT_EVENTS)
    parser.add_argument("--provenance", type=Path, default=DEFAULT_PROVENANCE)
    parser.add_argument("--audio-root", type=Path, default=PROJECT_ROOT / "logs" / "audio_dump")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--sensevoice-result", type=Path)
    parser.add_argument("--faster-whisper-result", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_ANALYSIS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.mode == "build":
            payload = build_manifest(
                events_path=args.events,
                provenance_path=args.provenance,
                audio_root=args.audio_root,
            )
            output = args.manifest
        else:
            if args.sensevoice_result is None or args.faster_whisper_result is None:
                raise ValueError("analyze mode requires both ASR result paths")
            payload = analyze_dual_replay(
                manifest_path=args.manifest,
                sensevoice_path=args.sensevoice_result,
                faster_whisper_path=args.faster_whisper_result,
            )
            output = args.output
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Weak-signal dual-ASR evaluation failed: {exc}", file=sys.stderr)
        return 1
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(payload.get('cases') or [])} cases to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
