import numpy as np

from scripts.replay_phase0_speaker_similarity import (
    best_low_similarity_threshold,
    live_gate_readiness,
    loco_safety_first,
    loco_target_speaker,
    _prediction_error_summary,
)


def test_best_low_similarity_threshold_separates_suppress():
    similarities = np.asarray([0.9, 0.8, 0.2, 0.1])
    labels = np.asarray([0, 0, 1, 1])

    threshold = best_low_similarity_threshold(similarities, labels)

    assert 0.2 < threshold < 0.8


def test_loco_target_speaker_uses_other_case_references():
    embeddings = np.asarray(
        [
            [1.0, 0.0],
            [1.0, 0.1],
            [0.0, 1.0],
            [0.1, 1.0],
            [0.9, 0.0],
            [0.0, 0.9],
        ],
        dtype=np.float32,
    )
    embeddings /= np.linalg.norm(embeddings, axis=1, keepdims=True)
    labels = np.asarray([0, 0, 1, 1, 0, 1])
    case_ids = ["a", "b", "a", "b", "c", "c"]

    metrics, rows = loco_target_speaker(embeddings, labels, case_ids)

    assert metrics["balanced_accuracy"] == 1.0
    assert [row["predicted"] for row in rows] == [
        "pass",
        "pass",
        "suppress",
        "suppress",
        "pass",
        "suppress",
    ]

    safety_embeddings = np.asarray(
        [[1.0, 0.0], [1.0, 0.0], [0.0, 1.0], [0.0, 1.0], [1.0, 0.0], [0.0, 1.0]],
        dtype=np.float32,
    )
    safety_metrics, _ = loco_safety_first(safety_embeddings, labels, case_ids)
    assert safety_metrics["host_pass_recall"] == 1.0


def test_prediction_error_summary_uses_live_gate_error_names():
    labels = np.asarray([0, 1])
    prediction_rows = [
        {"predicted": "suppress", "host_similarity": 0.4, "training_threshold": 0.5},
        {"predicted": "pass", "host_similarity": 0.6, "training_threshold": 0.5},
    ]
    span_rows = [
        {"span_key": "S1:utt-1:0", "sample_id": "S1", "source_class": "host"},
        {"span_key": "S2:utt-2:0", "sample_id": "S2", "source_class": "other"},
    ]

    summary = _prediction_error_summary(labels, prediction_rows, span_rows)

    assert summary["host_false_suppression_count"] == 1
    assert summary["host_false_suppressions"][0] == {
        "index": 0,
        "span_key": "S1:utt-1:0",
        "sample_id": "S1",
        "source_class": "host",
        "expected": "pass",
        "predicted": "suppress",
        "host_similarity": 0.4,
        "training_threshold": 0.5,
    }
    assert summary["nonhost_false_pass_count"] == 1
    assert summary["nonhost_false_passes"][0]["span_key"] == "S2:utt-2:0"


def test_live_gate_readiness_rejects_policies_with_any_shadow_error():
    labels = np.asarray([0, 1])
    span_rows = [
        {"span_key": "S1:utt-1:0", "sample_id": "S1", "source_class": "host"},
        {"span_key": "S2:utt-2:0", "sample_id": "S2", "source_class": "other"},
    ]

    readiness = live_gate_readiness(
        labels,
        span_rows,
        balanced_loco_predictions=[
            {"predicted": "suppress"},
            {"predicted": "suppress"},
        ],
        safety_first_predictions=[
            {"predicted": "pass"},
            {"predicted": "pass"},
        ],
    )

    assert readiness["status"] == "not_ready"
    assert readiness["candidate_policies"] == []
    assert readiness["policies"]["balanced_loco"]["host_false_suppression_count"] == 1
    assert readiness["policies"]["balanced_loco"]["nonhost_false_pass_count"] == 0
    assert readiness["policies"]["safety_first"]["host_false_suppression_count"] == 0
    assert readiness["policies"]["safety_first"]["nonhost_false_pass_count"] == 1


def test_live_gate_readiness_accepts_error_free_policy_candidate():
    labels = np.asarray([0, 1])
    span_rows = [
        {"span_key": "S1:utt-1:0", "sample_id": "S1", "source_class": "host"},
        {"span_key": "S2:utt-2:0", "sample_id": "S2", "source_class": "other"},
    ]

    readiness = live_gate_readiness(
        labels,
        span_rows,
        balanced_loco_predictions=[
            {"predicted": "pass"},
            {"predicted": "suppress"},
        ],
        safety_first_predictions=[
            {"predicted": "pass"},
            {"predicted": "pass"},
        ],
    )

    assert readiness["status"] == "candidate"
    assert readiness["candidate_policies"] == ["balanced_loco"]
    assert readiness["policies"]["balanced_loco"]["status"] == "candidate"
    assert readiness["policies"]["safety_first"]["status"] == "not_ready"
