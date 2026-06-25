import hashlib
import json

import numpy as np
import soundfile as sf

from scripts.routing_span_annotations import RoutingAnnotationStore, build_routing_tasks, load_manifest
from scripts.summarize_phase0_routing_spans import build_summary


def _write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_build_summary_validates_complete_cases_and_mechanisms(tmp_path):
    audio_path = tmp_path / "audio.wav"
    sf.write(audio_path, np.zeros(16000, dtype=np.float32), 16000)
    raw = audio_path.read_bytes()
    manifest_path = tmp_path / "manifest.json"
    annotation_path = tmp_path / "routing.annotations.json"
    manifest = {
        "cases": [
            {
                "sample_id": "S001",
                "root_cause_group": "source_routing",
                "annotation": {"label": "b_stt_error"},
                "audio_assets": [
                    {
                        "utterance_id": "utt-1",
                        "chunk_role": "primary",
                        "source_kind": "current",
                        "audio_path": str(audio_path),
                        "size_bytes": len(raw),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                    }
                ],
                "sample": {
                    "source_text": "source",
                    "target_text": "target",
                    "source_chunks": [{"utterance_id": "utt-1", "stt_audio_seconds": 1.0}],
                },
            }
        ]
    }
    _write_json(manifest_path, manifest)
    tasks = build_routing_tasks(load_manifest(manifest_path), project_root=tmp_path)
    store = RoutingAnnotationStore(
        path=annotation_path,
        manifest_path=manifest_path,
        tasks=tasks,
    )
    store.update(
        {
            "sample_id": "S001",
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
                            "end_seconds": 0.9,
                            "source_class": "mixed",
                            "routing_action": "extract_host",
                        },
                        {
                            "start_seconds": 0.9,
                            "end_seconds": 1.0,
                            "source_class": "uncertain",
                            "routing_action": "exclude",
                        },
                    ]
                }
            },
        }
    )

    summary = build_summary(
        manifest_path=manifest_path,
        annotation_path=annotation_path,
        project_root=tmp_path,
    )

    assert summary["mechanism_counts"] == {"overlap_extraction": 1}
    assert summary["evaluable_seconds"] == 0.9
    assert summary["excluded_uncertain_seconds"] == 0.1
    assert summary["routing_action_seconds"]["extract_host"] == 0.5
    assert summary["manifest_sha256"] == hashlib.sha256(
        manifest_path.read_bytes()
    ).hexdigest()
    assert summary["annotation_sha256"] == hashlib.sha256(
        annotation_path.read_bytes()
    ).hexdigest()
