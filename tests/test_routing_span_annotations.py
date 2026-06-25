import hashlib
import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np
import pytest
import soundfile as sf

from scripts.routing_span_annotations import (
    RoutingAnnotationStore,
    build_routing_tasks,
    coverage_gaps,
    load_manifest,
    normalize_spans,
)
from scripts.routing_span_review_server import RoutingHTTPServer


def _write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _manifest(audio_path):
    data = audio_path.read_bytes()
    return {
        "phase0_replay_manifest_schema": 2,
        "cases": [
            {
                "sample_id": "S001",
                "root_cause_group": "source_routing",
                "annotation": {
                    "label": "b_stt_error",
                    "speaker_source_tags": ["wrong_speaker_selected"],
                    "context_tags": ["multi_speaker"],
                },
                "audio_assets": [
                    {
                        "utterance_id": "utt-1",
                        "chunk_role": "primary",
                        "source_kind": "current",
                        "audio_path": str(audio_path),
                        "size_bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                ],
                "sample": {
                    "run_id": "run-a",
                    "sequence_id": 1,
                    "source_text": "source",
                    "target_text": "target",
                    "source_utterance_ids": ["utt-1"],
                    "evidence_source_utterance_ids": [],
                    "source_chunk_usages": [{"utterance_id": "utt-1", "role": "primary"}],
                    "source_chunks": [
                        {
                            "utterance_id": "utt-1",
                            "stt_audio_seconds": 1.0,
                        }
                    ],
                },
            },
            {
                "sample_id": "S002",
                "root_cause_group": "control_ok",
                "annotation": {"label": "ok"},
                "audio_assets": [],
                "sample": {},
            },
        ],
    }


def _span(start=0.0, end=1.0, source_class="host", routing_action="translate"):
    return {
        "start_seconds": start,
        "end_seconds": end,
        "source_class": source_class,
        "routing_action": routing_action,
        "notes": "",
    }


def _post_json(url, payload):
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def test_build_routing_tasks_only_uses_source_routing(tmp_path):
    audio_path = tmp_path / "audio.wav"
    sf.write(audio_path, np.zeros(16000, dtype=np.float32), 16000)

    tasks = build_routing_tasks(_manifest(audio_path), project_root=tmp_path)

    assert [task["sample_id"] for task in tasks] == ["S001"]
    assert tasks[0]["audio_assets"][0]["duration_seconds"] == 1.0
    assert tasks[0]["source_chunk_usages"]


def test_normalize_spans_rejects_overlap_and_reports_gaps():
    with pytest.raises(ValueError, match="must not overlap"):
        normalize_spans([_span(0, 0.7), _span(0.5, 1.0)], duration_seconds=1.0)

    spans = normalize_spans([_span(0.2, 0.8)], duration_seconds=1.0)

    assert coverage_gaps(spans, duration_seconds=1.0) == [
        {"start_seconds": 0.0, "end_seconds": 0.2},
        {"start_seconds": 0.8, "end_seconds": 1.0},
    ]


def test_store_allows_draft_but_requires_complete_coverage(tmp_path):
    audio_path = tmp_path / "audio.wav"
    sf.write(audio_path, np.zeros(16000, dtype=np.float32), 16000)
    manifest_path = tmp_path / "manifest.json"
    annotation_path = tmp_path / "routing.annotations.json"
    _write_json(manifest_path, _manifest(audio_path))
    tasks = build_routing_tasks(load_manifest(manifest_path), project_root=tmp_path)
    store = RoutingAnnotationStore(
        path=annotation_path,
        manifest_path=manifest_path,
        tasks=tasks,
    )
    partial = {
        "sample_id": "S001",
        "status": "draft",
        "assets": {"utt-1": {"spans": [_span(0.2, 0.8)]}},
    }

    draft = store.update(partial)
    assert len(draft["assets"]["utt-1"]["coverage_gaps"]) == 2
    partial["status"] = "complete"
    with pytest.raises(ValueError, match="incomplete time coverage"):
        store.update(partial)

    complete = store.update(
        {
            "sample_id": "S001",
            "status": "complete",
            "notes": "done",
            "assets": {"utt-1": {"spans": [_span()]}},
        }
    )
    assert complete["status"] == "complete"
    assert complete["assets"]["utt-1"]["coverage_gaps"] == []
    assert json.loads(annotation_path.read_text(encoding="utf-8"))["manifest_sha256"]


def test_store_rejects_annotations_for_changed_manifest(tmp_path):
    audio_path = tmp_path / "audio.wav"
    sf.write(audio_path, np.zeros(16000, dtype=np.float32), 16000)
    manifest_path = tmp_path / "manifest.json"
    annotation_path = tmp_path / "routing.annotations.json"
    _write_json(manifest_path, _manifest(audio_path))
    tasks = build_routing_tasks(load_manifest(manifest_path), project_root=tmp_path)
    RoutingAnnotationStore(path=annotation_path, manifest_path=manifest_path, tasks=tasks).update(
        {"sample_id": "S001", "status": "draft", "assets": {"utt-1": {"spans": []}}}
    )
    changed = _manifest(audio_path)
    changed["cases"][0]["sample"]["source_text"] = "changed"
    _write_json(manifest_path, changed)

    with pytest.raises(ValueError, match="manifest fingerprint"):
        RoutingAnnotationStore(path=annotation_path, manifest_path=manifest_path, tasks=tasks)


def test_http_state_save_and_audio(tmp_path):
    audio_path = tmp_path / "audio.wav"
    sf.write(audio_path, np.zeros(16000, dtype=np.float32), 16000)
    manifest_path = tmp_path / "manifest.json"
    annotation_path = tmp_path / "routing.annotations.json"
    _write_json(manifest_path, _manifest(audio_path))
    server = RoutingHTTPServer(
        ("127.0.0.1", 0),
        manifest_path=manifest_path,
        annotation_path=annotation_path,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    base = f"http://{host}:{port}"
    try:
        with urlopen(f"{base}/api/state", timeout=5) as response:
            state = json.loads(response.read().decode("utf-8"))
        assert len(state["tasks"]) == 1
        assert state["source_classes"] == [
            "host",
            "content_other",
            "alert_tts",
            "mixed",
            "unrelated",
            "uncertain",
        ]

        saved = _post_json(
            f"{base}/api/annotation",
            {
                "sample_id": "S001",
                "status": "complete",
                "assets": {"utt-1": {"spans": [_span()]}},
            },
        )
        assert saved["annotation"]["status"] == "complete"
        with urlopen(f"{base}/audio/S001-1.wav", timeout=5) as response:
            assert response.headers["Content-Type"] == "audio/wav"

        audio_path.write_bytes(b"changed after server startup")
        with pytest.raises(HTTPError) as changed_audio:
            urlopen(f"{base}/audio/S001-1.wav", timeout=5)
        assert changed_audio.value.code == 409

        bad_request = Request(
            f"{base}/api/annotation",
            data=json.dumps(
                {
                    "sample_id": "S001",
                    "status": "complete",
                    "assets": {"utt-1": {"spans": [_span(0.2, 0.8)]}},
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(HTTPError) as exc:
            urlopen(bad_request, timeout=5)
        assert exc.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
