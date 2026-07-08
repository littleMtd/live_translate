from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.evaluate_phase0_alert_shadow import binary_metrics
from scripts.replay_phase0_stt_candidates import verify_audio_asset
from scripts.routing_span_annotations import load_manifest, sha256_file


DEFAULT_DATASET = PROJECT_ROOT / "scratch" / "analysis" / "phase0_alert_shadow_dataset_20260624.json"
DEFAULT_MANIFEST = PROJECT_ROOT / "scratch" / "analysis" / "phase0_replay_manifest_20260624.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "scratch" / "analysis" / "phase0_speaker_similarity_shadow_20260624.json"
MODEL_ID = "iic/speech_campplus_sv_zh-cn_16k-common"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"failed to read JSON {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object in {path}")
    return data


def _normalize(vector: np.ndarray) -> np.ndarray:
    values = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(values))
    if norm < 1e-9:
        raise ValueError("speaker embedding has zero norm")
    return values / norm


def best_low_similarity_threshold(similarities: np.ndarray, labels: np.ndarray) -> float:
    unique = np.unique(similarities)
    thresholds = [float(unique[0] - 1e-9), float(unique[-1] + 1e-9)]
    thresholds.extend(float((left + right) / 2.0) for left, right in zip(unique[:-1], unique[1:]))
    best_threshold = thresholds[0]
    best_balanced = -1.0
    for threshold in thresholds:
        predictions = (similarities <= threshold).astype(int)
        balanced = float(binary_metrics(labels, predictions)["balanced_accuracy"])
        if balanced > best_balanced:
            best_balanced = balanced
            best_threshold = threshold
    return best_threshold


def _prediction_value(row: dict[str, Any]) -> int:
    predicted = str(row.get("predicted", ""))
    if predicted == "suppress":
        return 1
    if predicted == "pass":
        return 0
    raise ValueError(f"unexpected prediction label: {predicted!r}")


def _label_name(value: int) -> str:
    return "suppress" if int(value) else "pass"


def _prediction_error_summary(
    labels: np.ndarray,
    prediction_rows: list[dict[str, Any]],
    span_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Summarize class-specific errors in live-gate terms.

    `suppress` is the positive class for metrics, but the deployment-critical
    failure is usually a host span being incorrectly suppressed. Spell that out
    directly so the artifact cannot be mistaken for live-gate-ready based only on
    balanced accuracy.
    """
    if len(labels) != len(prediction_rows) or len(labels) != len(span_rows):
        raise ValueError("labels, prediction rows, and span rows must have the same length")

    host_false_suppressions: list[dict[str, Any]] = []
    nonhost_false_passes: list[dict[str, Any]] = []
    for index, (expected_value, prediction, span) in enumerate(
        zip(labels, prediction_rows, span_rows)
    ):
        predicted_value = _prediction_value(prediction)
        if int(expected_value) == predicted_value:
            continue
        item = {
            "index": int(index),
            "span_key": span.get("span_key"),
            "sample_id": span.get("sample_id"),
            "source_class": span.get("source_class"),
            "expected": _label_name(int(expected_value)),
            "predicted": _label_name(predicted_value),
        }
        for key in ("host_similarity", "training_threshold", "training_host_floor_threshold"):
            if key in prediction:
                item[key] = prediction[key]
        if int(expected_value) == 0 and predicted_value == 1:
            host_false_suppressions.append(item)
        elif int(expected_value) == 1 and predicted_value == 0:
            nonhost_false_passes.append(item)

    return {
        "host_false_suppression_count": len(host_false_suppressions),
        "host_false_suppressions": host_false_suppressions,
        "nonhost_false_pass_count": len(nonhost_false_passes),
        "nonhost_false_passes": nonhost_false_passes,
    }


def live_gate_readiness(
    labels: np.ndarray,
    span_rows: list[dict[str, Any]],
    *,
    balanced_loco_predictions: list[dict[str, Any]],
    safety_first_predictions: list[dict[str, Any]],
) -> dict[str, Any]:
    policies = {
        "balanced_loco": _prediction_error_summary(
            labels,
            balanced_loco_predictions,
            span_rows,
        ),
        "safety_first": _prediction_error_summary(
            labels,
            safety_first_predictions,
            span_rows,
        ),
    }
    criteria = (
        "candidate only if a policy has zero host_false_suppressions and zero "
        "nonhost_false_passes in leave-one-case-out shadow evaluation"
    )
    for summary in policies.values():
        summary["status"] = (
            "candidate"
            if summary["host_false_suppression_count"] == 0
            and summary["nonhost_false_pass_count"] == 0
            else "not_ready"
        )
    candidate_policies = [name for name, summary in policies.items() if summary["status"] == "candidate"]
    return {
        "status": "candidate" if candidate_policies else "not_ready",
        "criteria": criteria,
        "candidate_policies": candidate_policies,
        "policies": policies,
    }


def loco_target_speaker(
    embeddings: np.ndarray,
    labels: np.ndarray,
    case_ids: list[str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    predictions = np.full(len(labels), -1, dtype=int)
    rows: list[dict[str, Any]] = []
    for case_id in sorted(set(case_ids)):
        test_mask = np.asarray([value == case_id for value in case_ids])
        train_mask = ~test_mask
        train_labels = labels[train_mask]
        if set(train_labels.tolist()) != {0, 1}:
            raise ValueError(f"LOCO speaker fold lacks both classes: {case_id}")
        train_embeddings = embeddings[train_mask]
        host_centroid = _normalize(np.mean(train_embeddings[train_labels == 0], axis=0))
        train_similarity = train_embeddings @ host_centroid
        threshold = best_low_similarity_threshold(train_similarity, train_labels)
        test_indices = np.where(test_mask)[0]
        test_similarity = embeddings[test_mask] @ host_centroid
        test_predictions = (test_similarity <= threshold).astype(int)
        predictions[test_mask] = test_predictions
        for index, similarity, prediction in zip(test_indices, test_similarity, test_predictions):
            rows.append(
                {
                    "index": int(index),
                    "case_id": case_id,
                    "host_similarity": round(float(similarity), 6),
                    "training_threshold": round(float(threshold), 6),
                    "predicted": "suppress" if prediction else "pass",
                }
            )
    rows.sort(key=lambda item: item["index"])
    return binary_metrics(labels, predictions), rows


def loco_safety_first(
    embeddings: np.ndarray,
    labels: np.ndarray,
    case_ids: list[str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    predictions = np.full(len(labels), -1, dtype=int)
    rows: list[dict[str, Any]] = []
    for case_id in sorted(set(case_ids)):
        test_mask = np.asarray([value == case_id for value in case_ids])
        train_mask = ~test_mask
        train_labels = labels[train_mask]
        train_embeddings = embeddings[train_mask]
        if not np.any(train_labels == 0):
            raise ValueError(f"safety fold has no host references: {case_id}")
        host_centroid = _normalize(np.mean(train_embeddings[train_labels == 0], axis=0))
        train_similarity = train_embeddings @ host_centroid
        # Calibrate without holdout labels: suppress only below every training-host score.
        threshold = float(np.min(train_similarity[train_labels == 0]) - 1e-6)
        test_indices = np.where(test_mask)[0]
        test_similarity = embeddings[test_mask] @ host_centroid
        test_predictions = (test_similarity <= threshold).astype(int)
        predictions[test_mask] = test_predictions
        for index, similarity, prediction in zip(test_indices, test_similarity, test_predictions):
            rows.append(
                {
                    "index": int(index),
                    "case_id": case_id,
                    "host_similarity": round(float(similarity), 6),
                    "training_host_floor_threshold": round(threshold, 6),
                    "predicted": "suppress" if prediction else "pass",
                }
            )
    rows.sort(key=lambda item: item["index"])
    return binary_metrics(labels, predictions), rows


def _resolve_model_path(value: str) -> Path | str:
    explicit = Path(value).expanduser()
    if explicit.is_dir():
        return explicit
    cached = (
        Path.home()
        / ".cache"
        / "modelscope"
        / "hub"
        / "models"
        / "iic"
        / "speech_campplus_sv_zh-cn_16k-common"
    )
    if value == MODEL_ID and (cached / "campplus_cn_common.bin").is_file():
        return cached
    return value


def replay_embeddings(
    *,
    dataset: dict[str, Any],
    manifest: dict[str, Any],
    model: Any,
    project_root: Path,
) -> tuple[np.ndarray, list[dict[str, Any]]]:
    cases = {str(case.get("sample_id") or ""): case for case in manifest["cases"] if isinstance(case, dict)}
    audio_cache: dict[tuple[str, str], tuple[np.ndarray, int]] = {}
    vectors: list[np.ndarray] = []
    rows: list[dict[str, Any]] = []
    for span in dataset["spans"]:
        sample_id = str(span["sample_id"])
        utterance_id = str(span["utterance_id"])
        cache_key = (sample_id, utterance_id)
        if cache_key not in audio_cache:
            case = cases[sample_id]
            asset = next(
                asset for asset in case["audio_assets"] if asset["utterance_id"] == utterance_id
            )
            audio_path = Path(str(asset["audio_path"]))
            if not audio_path.is_absolute():
                audio_path = project_root / audio_path
            verify_audio_asset(asset, audio_path)
            audio, sample_rate = sf.read(audio_path, dtype="float32", always_2d=False)
            if sample_rate != 16000 or audio.ndim != 1:
                raise ValueError(f"expected 16 kHz mono audio: {audio_path}")
            audio_cache[cache_key] = (np.asarray(audio, dtype=np.float32), sample_rate)
        audio, sample_rate = audio_cache[cache_key]
        start = round(float(span["start_seconds"]) * sample_rate)
        end = round(float(span["end_seconds"]) * sample_rate)
        clip = audio[start:end]
        started = time.monotonic()
        result = model.generate(input=clip)
        latency_ms = round((time.monotonic() - started) * 1000, 1)
        if not result or "spk_embedding" not in result[0]:
            raise ValueError(f"speaker model returned no embedding: {span['span_key']}")
        embedding_value = result[0]["spk_embedding"]
        if hasattr(embedding_value, "detach"):
            embedding_value = embedding_value.detach().cpu().numpy()
        embedding = _normalize(np.asarray(embedding_value))
        vectors.append(embedding)
        rows.append(
            {
                "span_key": span["span_key"],
                "sample_id": sample_id,
                "source_class": span["source_class"],
                "binary_target": span["binary_target"],
                "duration_seconds": span["duration_seconds"],
                "embedding_dimension": int(embedding.size),
                "embedding": [round(float(value), 8) for value in embedding],
                "latency_ms": latency_ms,
            }
        )
    return np.vstack(vectors), rows


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run CAM++ target-speaker shadow on Phase 0 spans.")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--model", default=MODEL_ID)
    parser.add_argument("--device", default="cuda")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        dataset = _read_json(args.dataset)
        manifest = load_manifest(args.manifest)
        if dataset.get("manifest_sha256") != sha256_file(args.manifest):
            raise ValueError("alert dataset manifest fingerprint does not match")
        from funasr import AutoModel

        model_path = _resolve_model_path(args.model)
        model = AutoModel(model=str(model_path), device=args.device, disable_update=True)
        embeddings, rows = replay_embeddings(
            dataset=dataset,
            manifest=manifest,
            model=model,
            project_root=PROJECT_ROOT,
        )
        labels = np.asarray(
            [1 if span["binary_target"] == "suppress" else 0 for span in dataset["spans"]]
        )
        case_ids = [str(span["sample_id"]) for span in dataset["spans"]]
        metrics, predictions = loco_target_speaker(embeddings, labels, case_ids)
        safety_metrics, safety_predictions = loco_safety_first(embeddings, labels, case_ids)
    except (OSError, ValueError) as exc:
        print(f"Speaker similarity shadow failed: {exc}", file=sys.stderr)
        return 1

    readiness = live_gate_readiness(
        labels,
        rows,
        balanced_loco_predictions=predictions,
        safety_first_predictions=safety_predictions,
    )

    for row, prediction, safety_prediction, expected in zip(rows, predictions, safety_predictions, labels):
        row.update(prediction)
        row["expected"] = _label_name(int(expected))
        row["correct"] = row["expected"] == row["predicted"]
        row["safety_first"] = {
            "host_similarity": safety_prediction["host_similarity"],
            "training_host_floor_threshold": safety_prediction[
                "training_host_floor_threshold"
            ],
            "predicted": safety_prediction["predicted"],
            "correct": row["expected"] == safety_prediction["predicted"],
        }
    model_file = Path(model_path) / "campplus_cn_common.bin" if isinstance(model_path, Path) else None
    output = {
        "phase0_speaker_similarity_shadow_schema": 2,
        "dataset_path": str(args.dataset),
        "dataset_sha256": sha256_file(args.dataset),
        "model_id": MODEL_ID,
        "model_path": str(model_path),
        "model_sha256": sha256_file(model_file) if model_file and model_file.is_file() else None,
        "device": args.device,
        "case_count": len(set(case_ids)),
        "span_count": len(rows),
        "loco_metrics": metrics,
        "loco_safety_first": {
            "calibration": "threshold below minimum training-host similarity; no holdout labels",
            "metrics": safety_metrics,
            "predictions": safety_predictions,
        },
        "live_gate_readiness": readiness,
        "spans": rows,
        "interpretation": (
            "Each holdout case uses only host embeddings and threshold selection from other cases. "
            "Use live_gate_readiness, not balanced_accuracy alone, to decide whether a "
            "speaker-similarity policy is safe enough for live suppression."
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"CAM++ LOCO balanced_accuracy={metrics['balanced_accuracy']:.3f} "
        f" suppress_recall={metrics['suppress_recall']:.3f} "
        f"host_pass_recall={metrics['host_pass_recall']:.3f}; "
        f"safety suppress_recall={safety_metrics['suppress_recall']:.3f} "
        f"host_pass_recall={safety_metrics['host_pass_recall']:.3f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
