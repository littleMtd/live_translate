import json
import hashlib

import numpy as np
import soundfile as sf

from scripts.replay_phase0_stt_candidates import (
    clean_sensevoice_text,
    replay_cases,
    select_cases,
    verify_audio_asset,
)


def test_clean_sensevoice_text_extracts_metadata_tokens():
    text, tags = clean_sensevoice_text(
        "<|ko|><|EMO_UNKNOWN|><|Speech|><|withitn|>맞아 어 맞아"
    )

    assert text == "맞아 어 맞아"
    assert tags == ["<|ko|>", "<|EMO_UNKNOWN|>", "<|Speech|>", "<|withitn|>"]


def test_select_cases_limits_groups_and_ids():
    manifest = {
        "cases": [
            {"sample_id": "S001", "root_cause_group": "clean_host_stt"},
            {"sample_id": "S002", "root_cause_group": "source_routing"},
        ]
    }

    selected = select_cases(manifest, groups={"clean_host_stt"}, case_ids={"S001"})

    assert [case["sample_id"] for case in selected] == ["S001"]


def test_replay_cases_orders_audio_and_preserves_candidates(tmp_path):
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    sf.write(first, np.zeros(1600, dtype=np.float32), 16000)
    sf.write(second, np.ones(3200, dtype=np.float32) * 0.01, 16000)
    case = {
        "sample_id": "S001",
        "root_cause_group": "clean_host_stt",
        "annotation": {"label": "b_stt_error"},
        "audio_assets": [
            {
                "utterance_id": "utt-2",
                "source_kind": "current",
                "audio_path": str(second),
                "size_bytes": second.stat().st_size,
                "sha256": hashlib.sha256(second.read_bytes()).hexdigest(),
            },
            {
                "utterance_id": "utt-1",
                "source_kind": "evidence",
                "audio_path": str(first),
                "size_bytes": first.stat().st_size,
                "sha256": hashlib.sha256(first.read_bytes()).hexdigest(),
            },
        ],
        "sample": {
            "source_text": "groq",
            "source_chunks": [
                {"utterance_id": "utt-2", "stt_event_line": 20},
                {"utterance_id": "utt-1", "stt_event_line": 10},
            ],
        },
    }
    outputs = iter(
        [
            "<|ko|><|Speech|>first",
            "<|ko|><|Speech|>second",
        ]
    )

    result = replay_cases([case], generate=lambda _audio: next(outputs), project_root=tmp_path)[0]

    assert [row["utterance_id"] for row in result["sensevoice_chunks"]] == ["utt-1", "utt-2"]
    assert result["sensevoice_text"] == "second"
    assert result["sensevoice_evidence_text"] == "first"
    assert result["sensevoice_chunks"][0]["audio_fingerprint_verified"] is True


def test_verify_audio_asset_rejects_changed_wav(tmp_path):
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"before")
    asset = {
        "size_bytes": audio_path.stat().st_size,
        "sha256": hashlib.sha256(audio_path.read_bytes()).hexdigest(),
    }
    audio_path.write_bytes(b"after!")

    try:
        verify_audio_asset(asset, audio_path)
    except ValueError as exc:
        assert "sha256 changed" in str(exc)
    else:
        raise AssertionError("changed audio fingerprint was accepted")
