from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.replay_phase0_stt_candidates import verify_audio_asset
from scripts.routing_span_annotations import (
    coverage_gaps,
    load_manifest,
    normalize_spans,
    sha256_file,
)


DEFAULT_MANIFEST = PROJECT_ROOT / ".analysis-tmp" / "phase0_replay_manifest_20260624.json"
DEFAULT_ANNOTATIONS = PROJECT_ROOT / ".analysis-tmp" / "phase0_routing_spans_20260624.annotations.json"
DEFAULT_OUTPUT = PROJECT_ROOT / ".analysis-tmp" / "phase0_alert_shadow_dataset_20260624.json"
INCLUDED_CLASSES = {"host", "alert_tts", "unrelated"}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"failed to read JSON {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object in {path}")
    return data


def _sha256_samples(audio: np.ndarray) -> str:
    return hashlib.sha256(np.asarray(audio, dtype="<f4").tobytes()).hexdigest()


def _audio_features(audio: np.ndarray, sample_rate: int) -> dict[str, float]:
    values = np.asarray(audio, dtype=np.float32)
    if values.size == 0:
        raise ValueError("cannot compute features for an empty audio span")
    rms = float(np.sqrt(np.mean(values * values)))
    peak = float(np.max(np.abs(values)))
    dbfs = 20.0 * math.log10(max(rms, 1e-9))
    zcr = float(np.mean(np.signbit(values[1:]) != np.signbit(values[:-1]))) if values.size > 1 else 0.0

    window_size = min(1024, values.size)
    if window_size < 16:
        centroid = 0.0
        flatness = 0.0
    else:
        hop = max(1, window_size // 2)
        centroids: list[float] = []
        flatness_values: list[float] = []
        frequencies = np.fft.rfftfreq(window_size, d=1.0 / sample_rate)
        window = np.hanning(window_size).astype(np.float32)
        for offset in range(0, max(1, values.size - window_size + 1), hop):
            frame = values[offset : offset + window_size]
            if frame.size < window_size:
                frame = np.pad(frame, (0, window_size - frame.size))
            magnitude = np.abs(np.fft.rfft(frame * window)) + 1e-12
            total = float(np.sum(magnitude))
            centroids.append(float(np.sum(frequencies * magnitude) / total) / (sample_rate / 2.0))
            flatness_values.append(float(np.exp(np.mean(np.log(magnitude))) / np.mean(magnitude)))
        centroid = float(np.mean(centroids))
        flatness = float(np.mean(flatness_values))

    frame_size = max(1, int(sample_rate * 0.02))
    frame_rms = []
    for offset in range(0, values.size, frame_size):
        frame = values[offset : offset + frame_size]
        frame_rms.append(float(np.sqrt(np.mean(frame * frame))) if frame.size else 0.0)
    active_threshold = max(0.002, rms * 0.35)
    active_ratio = float(np.mean(np.asarray(frame_rms) >= active_threshold))
    return {
        "rms": round(rms, 8),
        "peak": round(peak, 8),
        "dbfs": round(dbfs, 4),
        "zero_crossing_rate": round(zcr, 6),
        "spectral_centroid_nyquist": round(centroid, 6),
        "spectral_flatness": round(flatness, 6),
        "active_frame_ratio": round(active_ratio, 6),
    }


def _chunk_diagnostics(sample: dict[str, Any], utterance_id: str) -> dict[str, Any]:
    chunks = sample.get("source_chunks")
    if not isinstance(chunks, list):
        return {}
    for chunk in chunks:
        if not isinstance(chunk, dict) or str(chunk.get("utterance_id") or "") != utterance_id:
            continue
        return {
            key: chunk.get(key)
            for key in (
                "chunk_role",
                "source_kind",
                "stt_status",
                "stt_reason",
                "stt_engine",
                "stt_model",
                "stt_audio_seconds",
                "stt_latency_ms",
                "avg_logprob",
                "no_speech_prob",
                "audio_context",
            )
        }
    return {}


def build_dataset(
    *,
    manifest_path: Path,
    annotation_path: Path,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    annotations_data = _read_json(annotation_path)
    if annotations_data.get("manifest_sha256") != sha256_file(manifest_path):
        raise ValueError("routing annotation manifest fingerprint does not match")
    annotations = annotations_data.get("annotations")
    if not isinstance(annotations, dict):
        raise ValueError("routing annotation file has no annotations object")

    spans: list[dict[str, Any]] = []
    excluded_counts: dict[str, int] = {}
    for case in manifest["cases"]:
        if not isinstance(case, dict) or case.get("root_cause_group") != "source_routing":
            continue
        sample_id = str(case.get("sample_id") or "")
        annotation = annotations.get(sample_id)
        if not isinstance(annotation, dict) or annotation.get("status") != "complete":
            raise ValueError(f"routing annotation is not complete: {sample_id}")
        sample = case.get("sample")
        if not isinstance(sample, dict):
            raise ValueError(f"invalid sample payload: {sample_id}")

        for asset in case.get("audio_assets", []):
            if not isinstance(asset, dict):
                continue
            utterance_id = str(asset.get("utterance_id") or "")
            audio_path = Path(str(asset.get("audio_path") or ""))
            if not audio_path.is_absolute():
                audio_path = project_root / audio_path
            verify_audio_asset(asset, audio_path)
            audio, sample_rate = sf.read(audio_path, dtype="float32", always_2d=False)
            if sample_rate != 16000 or audio.ndim != 1:
                raise ValueError(f"expected 16 kHz mono audio: {audio_path}")
            asset_annotation = annotation.get("assets", {}).get(utterance_id)
            if not isinstance(asset_annotation, dict):
                raise ValueError(f"missing asset annotation: {sample_id}/{utterance_id}")
            duration_seconds = len(audio) / sample_rate
            normalized_spans = normalize_spans(
                asset_annotation.get("spans"),
                duration_seconds=duration_seconds,
            )
            gaps = coverage_gaps(normalized_spans, duration_seconds=duration_seconds)
            if gaps:
                raise ValueError(
                    f"routing coverage gaps: {sample_id}/{utterance_id}: {gaps}"
                )

            for index, span in enumerate(normalized_spans, start=1):
                source_class = str(span.get("source_class") or "")
                if source_class not in INCLUDED_CLASSES:
                    excluded_counts[source_class] = excluded_counts.get(source_class, 0) + 1
                    continue
                start = float(span["start_seconds"])
                end = float(span["end_seconds"])
                start_sample = max(0, min(len(audio), round(start * sample_rate)))
                end_sample = max(start_sample, min(len(audio), round(end * sample_rate)))
                clip = np.asarray(audio[start_sample:end_sample], dtype=np.float32)
                if clip.size == 0:
                    raise ValueError(f"empty span after slicing: {sample_id}/{utterance_id}/{index}")
                expected_action = str(span.get("routing_action") or "")
                spans.append(
                    {
                        "span_key": f"{sample_id}:{utterance_id}:{index}",
                        "sample_id": sample_id,
                        "utterance_id": utterance_id,
                        "start_seconds": start,
                        "end_seconds": end,
                        "duration_seconds": round(clip.size / sample_rate, 3),
                        "source_class": source_class,
                        "expected_action": expected_action,
                        "binary_target": "pass" if expected_action == "translate" else "suppress",
                        "audio_sample_sha256": _sha256_samples(clip),
                        "features": _audio_features(clip, sample_rate),
                        "chunk_diagnostics": _chunk_diagnostics(sample, utterance_id),
                    }
                )

    class_counts: dict[str, int] = {}
    target_counts: dict[str, int] = {}
    for span in spans:
        class_counts[span["source_class"]] = class_counts.get(span["source_class"], 0) + 1
        target_counts[span["binary_target"]] = target_counts.get(span["binary_target"], 0) + 1
    return {
        "phase0_alert_shadow_dataset_schema": 1,
        "manifest_path": str(manifest_path),
        "manifest_sha256": sha256_file(manifest_path),
        "routing_annotation_path": str(annotation_path),
        "routing_annotation_sha256": sha256_file(annotation_path),
        "included_source_classes": sorted(INCLUDED_CLASSES),
        "span_count": len(spans),
        "source_class_counts": dict(sorted(class_counts.items())),
        "binary_target_counts": dict(sorted(target_counts.items())),
        "excluded_span_counts": dict(sorted(excluded_counts.items())),
        "spans": spans,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build non-overlap alert suppression shadow dataset.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--annotations", type=Path, default=DEFAULT_ANNOTATIONS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        dataset = build_dataset(
            manifest_path=args.manifest,
            annotation_path=args.annotations,
        )
    except ValueError as exc:
        print(f"Alert shadow dataset failed: {exc}", file=sys.stderr)
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(dataset, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"Wrote {dataset['span_count']} non-overlap spans to {args.output} "
        f"with targets {dataset['binary_target_counts']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
