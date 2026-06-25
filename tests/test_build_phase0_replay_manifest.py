import json

import pytest

from scripts.build_phase0_replay_manifest import build_replay_manifest


def _write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _sample(sample_id, audio_path, *, speaker_policy="host-primary"):
    return {
        "speaker_policy": speaker_policy,
        "samples": [
            {
                "sample_id": sample_id,
                "run_id": "run-a",
                "sequence_id": 1,
                "source_text": "source",
                "target_text": "target",
                "source_utterance_ids": ["utt-1"],
                "evidence_source_utterance_ids": [],
                "source_chunk_usages": [{"utterance_id": "utt-1", "usage": "current"}],
                "source_chunks": [
                    {
                        "utterance_id": "utt-1",
                        "chunk_role": "primary",
                        "source_kind": "current",
                        "audio_path": str(audio_path),
                    }
                ],
            }
        ],
    }


def test_build_replay_manifest_classifies_and_hashes_audio(tmp_path):
    audio_path = tmp_path / "utt-1.wav"
    audio_path.write_bytes(b"RIFF-test")
    routing_path = tmp_path / "routing.json"
    host_path = tmp_path / "host.json"
    annotations_path = tmp_path / "annotations.json"
    _write_json(routing_path, _sample("S001", audio_path))
    _write_json(host_path, _sample("S002", audio_path))
    _write_json(
        annotations_path,
        {
            "annotations": {
                "S001": {
                    "label": "b_stt_error",
                    "speaker_source_tags": ["wrong_speaker_selected"],
                    "context_tags": ["multi_speaker"],
                },
                "S002": {
                    "label": "b_stt_error",
                    "speaker_source_tags": ["host_only"],
                    "context_tags": [],
                },
            }
        },
    )

    manifest = build_replay_manifest(
        sample_paths=[routing_path, host_path],
        annotation_path=annotations_path,
        project_root=tmp_path,
    )

    assert manifest["case_count"] == 2
    assert manifest["root_cause_group_counts"] == {"clean_host_stt": 1, "source_routing": 1}
    assert manifest["cases"][0]["ground_truth_status"] == "human_judgment_only"
    assert manifest["cases"][0]["audio_assets"][0]["audio_path"] == "utt-1.wav"
    assert len(manifest["cases"][0]["audio_assets"][0]["sha256"]) == 64
    assert manifest["cases"][0]["sample"]["source_chunk_usages"]


def test_build_replay_manifest_rejects_unlabeled_case(tmp_path):
    audio_path = tmp_path / "utt-1.wav"
    audio_path.write_bytes(b"RIFF")
    sample_path = tmp_path / "sample.json"
    annotations_path = tmp_path / "annotations.json"
    _write_json(sample_path, _sample("S001", audio_path))
    _write_json(annotations_path, {"annotations": {"S001": {"label": ""}}})

    with pytest.raises(ValueError, match="missing completed annotation"):
        build_replay_manifest(sample_paths=[sample_path], annotation_path=annotations_path)


def test_build_replay_manifest_rejects_duplicate_ids(tmp_path):
    audio_path = tmp_path / "utt-1.wav"
    audio_path.write_bytes(b"RIFF")
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    annotations_path = tmp_path / "annotations.json"
    _write_json(first_path, _sample("S001", audio_path))
    _write_json(second_path, _sample("S001", audio_path))
    _write_json(
        annotations_path,
        {"annotations": {"S001": {"label": "ok", "speaker_source_tags": ["host_only"]}}},
    )

    with pytest.raises(ValueError, match="duplicate sample_id"):
        build_replay_manifest(
            sample_paths=[first_path, second_path],
            annotation_path=annotations_path,
        )


def test_build_replay_manifest_separates_filter_policy_from_translation(tmp_path):
    audio_path = tmp_path / "utt-1.wav"
    audio_path.write_bytes(b"RIFF")
    sample_path = tmp_path / "sample.json"
    annotations_path = tmp_path / "annotations.json"
    sample = _sample("S001", audio_path)
    sample["samples"][0].update(
        {"status": "filtered", "filter_reason": "stt_garbage", "target_text": None}
    )
    _write_json(sample_path, sample)
    _write_json(
        annotations_path,
        {
            "annotations": {
                "S001": {
                    "label": "a_translation_error",
                    "speaker_source_tags": ["host_only"],
                    "heard_source_text": "맞아 어 맞아",
                }
            }
        },
    )

    manifest = build_replay_manifest(
        sample_paths=[sample_path], annotation_path=annotations_path, project_root=tmp_path
    )

    assert manifest["root_cause_group_counts"] == {"filter_policy": 1}
    assert manifest["cases"][0]["ground_truth_status"] == "exact_heard_source"
