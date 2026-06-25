from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = PROJECT_ROOT / ".analysis-tmp" / "phase0_alert_shadow_dataset_20260624.json"
DEFAULT_OUTPUT = PROJECT_ROOT / ".analysis-tmp" / "phase0_alert_shadow_evaluation_20260624.json"
ACOUSTIC_FEATURES = (
    "dbfs",
    "zero_crossing_rate",
    "spectral_centroid_nyquist",
    "spectral_flatness",
    "active_frame_ratio",
)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"failed to read dataset {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("spans"), list):
        raise ValueError("alert shadow dataset must contain spans")
    return data


def binary_metrics(y_true: np.ndarray, y_pred: np.ndarray) -> dict[str, float | int]:
    tp = int(np.sum((y_true == 1) & (y_pred == 1)))
    tn = int(np.sum((y_true == 0) & (y_pred == 0)))
    fp = int(np.sum((y_true == 0) & (y_pred == 1)))
    fn = int(np.sum((y_true == 1) & (y_pred == 0)))
    recall = tp / (tp + fn) if tp + fn else 0.0
    specificity = tn / (tn + fp) if tn + fp else 0.0
    precision = tp / (tp + fp) if tp + fp else 0.0
    return {
        "tp": tp,
        "tn": tn,
        "fp": fp,
        "fn": fn,
        "accuracy": round((tp + tn) / max(1, len(y_true)), 6),
        "balanced_accuracy": round((recall + specificity) / 2.0, 6),
        "suppress_precision": round(precision, 6),
        "suppress_recall": round(recall, 6),
        "host_pass_recall": round(specificity, 6),
    }


def oriented_auc(values: np.ndarray, labels: np.ndarray) -> dict[str, Any]:
    positives = values[labels == 1]
    negatives = values[labels == 0]
    if not len(positives) or not len(negatives):
        raise ValueError("AUC requires both classes")
    wins = 0.0
    for positive in positives:
        for negative in negatives:
            if positive > negative:
                wins += 1.0
            elif positive == negative:
                wins += 0.5
    auc_high = wins / (len(positives) * len(negatives))
    if auc_high >= 0.5:
        return {"auc": round(auc_high, 6), "suppress_direction": "high"}
    return {"auc": round(1.0 - auc_high, 6), "suppress_direction": "low"}


def best_in_sample_threshold(values: np.ndarray, labels: np.ndarray) -> dict[str, Any]:
    unique = np.unique(values)
    thresholds = [float(unique[0] - 1e-9), float(unique[-1] + 1e-9)]
    thresholds.extend(float((left + right) / 2.0) for left, right in zip(unique[:-1], unique[1:]))
    best: dict[str, Any] | None = None
    for direction in ("high", "low"):
        for threshold in thresholds:
            prediction = (values >= threshold).astype(int) if direction == "high" else (values <= threshold).astype(int)
            metrics = binary_metrics(labels, prediction)
            candidate = {"threshold": threshold, "suppress_direction": direction, "metrics": metrics}
            if best is None or metrics["balanced_accuracy"] > best["metrics"]["balanced_accuracy"]:
                best = candidate
    assert best is not None
    best["threshold"] = round(float(best["threshold"]), 8)
    return best


def leave_one_case_out_nearest_centroid(
    matrix: np.ndarray,
    labels: np.ndarray,
    case_ids: list[str],
) -> tuple[dict[str, Any], np.ndarray]:
    predictions = np.full(len(labels), -1, dtype=int)
    for case_id in sorted(set(case_ids)):
        test_mask = np.asarray([value == case_id for value in case_ids])
        train_mask = ~test_mask
        train_y = labels[train_mask]
        if set(train_y.tolist()) != {0, 1}:
            raise ValueError(f"LOCO fold lacks both classes: {case_id}")
        train_x = matrix[train_mask]
        mean = np.mean(train_x, axis=0)
        std = np.std(train_x, axis=0)
        std[std < 1e-9] = 1.0
        normalized_train = (train_x - mean) / std
        normalized_test = (matrix[test_mask] - mean) / std
        pass_centroid = np.mean(normalized_train[train_y == 0], axis=0)
        suppress_centroid = np.mean(normalized_train[train_y == 1], axis=0)
        pass_distance = np.linalg.norm(normalized_test - pass_centroid, axis=1)
        suppress_distance = np.linalg.norm(normalized_test - suppress_centroid, axis=1)
        predictions[test_mask] = (suppress_distance < pass_distance).astype(int)
    return binary_metrics(labels, predictions), predictions


def evaluate_dataset(dataset: dict[str, Any]) -> dict[str, Any]:
    spans = dataset["spans"]
    if len(spans) < 2:
        raise ValueError("not enough spans to evaluate")
    labels = np.asarray([1 if span["binary_target"] == "suppress" else 0 for span in spans])
    case_ids = [str(span["sample_id"]) for span in spans]
    individual: dict[str, Any] = {}
    for feature in ACOUSTIC_FEATURES:
        values = np.asarray([float(span["features"][feature]) for span in spans], dtype=float)
        individual[feature] = {
            **oriented_auc(values, labels),
            "best_in_sample_threshold": best_in_sample_threshold(values, labels),
        }

    matrix = np.asarray(
        [[float(span["features"][feature]) for feature in ACOUSTIC_FEATURES] for span in spans],
        dtype=float,
    )
    loco_metrics, predictions = leave_one_case_out_nearest_centroid(matrix, labels, case_ids)
    prediction_rows = []
    for span, expected, predicted in zip(spans, labels, predictions):
        prediction_rows.append(
            {
                "span_key": span["span_key"],
                "sample_id": span["sample_id"],
                "source_class": span["source_class"],
                "expected": "suppress" if expected else "pass",
                "predicted": "suppress" if predicted else "pass",
                "correct": bool(expected == predicted),
            }
        )
    return {
        "phase0_alert_shadow_evaluation_schema": 1,
        "dataset_span_count": len(spans),
        "case_count": len(set(case_ids)),
        "positive_suppress_count": int(np.sum(labels == 1)),
        "negative_host_pass_count": int(np.sum(labels == 0)),
        "individual_feature_descriptive": individual,
        "loco_nearest_centroid": {
            "features": list(ACOUSTIC_FEATURES),
            "metrics": loco_metrics,
            "predictions": prediction_rows,
        },
        "interpretation": (
            "Individual thresholds are in-sample descriptive only. "
            "Use leave-one-case-out metrics for the cheap-signal gate."
        ),
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate cheap alert suppression shadow signals.")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        evaluation = evaluate_dataset(_read_json(args.dataset))
    except ValueError as exc:
        print(f"Alert shadow evaluation failed: {exc}", file=sys.stderr)
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evaluation, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    metrics = evaluation["loco_nearest_centroid"]["metrics"]
    print(
        f"LOCO balanced_accuracy={metrics['balanced_accuracy']:.3f} "
        f"suppress_recall={metrics['suppress_recall']:.3f} "
        f"host_pass_recall={metrics['host_pass_recall']:.3f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
