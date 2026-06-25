import hashlib
import json

import numpy as np
import soundfile as sf
import pytest

from scripts.build_phase0_alert_shadow_dataset import build_dataset
from scripts.routing_span_annotations import sha256_file


def _write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_build_dataset_slices_only_non_overlap_gate_classes(tmp_path):
    audio_path = tmp_path / "audio.wav"
    sf.write(audio_path, np.linspace(-0.1, 0.1, 16000, dtype=np.float32), 16000)
    raw = audio_path.read_bytes()
    manifest_path = tmp_path / "manifest.json"
    annotations_path = tmp_path / "routing.annotations.json"
    manifest = {
        "cases": [
            {
                "sample_id": "S001",
                "root_cause_group": "source_routing",
                "audio_assets": [
                    {
                        "utterance_id": "utt-1",
                        "audio_path": str(audio_path),
                        "size_bytes": len(raw),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                    }
                ],
                "sample": {
                    "source_chunks": [
                        {"utterance_id": "utt-1", "avg_logprob": -0.2, "no_speech_prob": 0.01}
                    ]
                },
            }
        ]
    }
    _write_json(manifest_path, manifest)
    annotations = {
        "manifest_sha256": sha256_file(manifest_path),
        "annotations": {
            "S001": {
                "status": "complete",
                "assets": {
                    "utt-1": {
                        "spans": [
                            {
                                "start_seconds": 0.0,
                                "end_seconds": 0.4,
                                "source_class": "host",
                                "routing_action": "translate",
                            },
                            {
                                "start_seconds": 0.4,
                                "end_seconds": 0.8,
                                "source_class": "alert_tts",
                                "routing_action": "suppress",
                            },
                            {
                                "start_seconds": 0.8,
                                "end_seconds": 1.0,
                                "source_class": "mixed",
                                "routing_action": "extract_host",
                            },
                        ]
                    }
                },
            }
        },
    }
    _write_json(annotations_path, annotations)

    dataset = build_dataset(
        manifest_path=manifest_path,
        annotation_path=annotations_path,
        project_root=tmp_path,
    )

    assert dataset["span_count"] == 2
    assert dataset["binary_target_counts"] == {"pass": 1, "suppress": 1}
    assert dataset["excluded_span_counts"] == {"mixed": 1}
    assert dataset["spans"][0]["chunk_diagnostics"]["avg_logprob"] == -0.2
    assert dataset["spans"][0]["features"]["rms"] > 0


def test_build_dataset_revalidates_annotation_spans(tmp_path):
    audio_path = tmp_path / "audio.wav"
    sf.write(audio_path, np.zeros(16000, dtype=np.float32), 16000)
    raw = audio_path.read_bytes()
    manifest_path = tmp_path / "manifest.json"
    annotations_path = tmp_path / "routing.annotations.json"
    manifest = {
        "cases": [
            {
                "sample_id": "S001",
                "root_cause_group": "source_routing",
                "audio_assets": [
                    {
                        "utterance_id": "utt-1",
                        "audio_path": str(audio_path),
                        "size_bytes": len(raw),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                    }
                ],
                "sample": {"source_chunks": [{"utterance_id": "utt-1"}]},
            }
        ]
    }
    _write_json(manifest_path, manifest)
    _write_json(
        annotations_path,
        {
            "manifest_sha256": sha256_file(manifest_path),
            "annotations": {
                "S001": {
                    "status": "complete",
                    "assets": {
                        "utt-1": {
                            "spans": [
                                {
                                    "start_seconds": 0.0,
                                    "end_seconds": 1.0,
                                    "source_class": "host",
                                    "routing_action": "invalid",
                                }
                            ]
                        }
                    },
                }
            },
        },
    )

    with pytest.raises(ValueError, match="unknown routing_action"):
        build_dataset(
            manifest_path=manifest_path,
            annotation_path=annotations_path,
            project_root=tmp_path,
        )
