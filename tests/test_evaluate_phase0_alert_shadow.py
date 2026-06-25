import numpy as np

from scripts.evaluate_phase0_alert_shadow import (
    binary_metrics,
    leave_one_case_out_nearest_centroid,
    oriented_auc,
)


def test_oriented_auc_handles_low_direction():
    values = np.asarray([10.0, 9.0, 1.0, 2.0])
    labels = np.asarray([0, 0, 1, 1])

    result = oriented_auc(values, labels)

    assert result == {"auc": 1.0, "suppress_direction": "low"}


def test_loco_nearest_centroid_separates_cases():
    matrix = np.asarray([[0.0], [0.1], [1.0], [1.1], [0.2], [0.9]])
    labels = np.asarray([0, 0, 1, 1, 0, 1])
    case_ids = ["a", "b", "a", "b", "c", "c"]

    metrics, predictions = leave_one_case_out_nearest_centroid(matrix, labels, case_ids)

    assert predictions.tolist() == labels.tolist()
    assert metrics == binary_metrics(labels, labels)
