import json

import pytest

from scripts.build_phase0_eval_candidates import build_phase0_candidates


def _write_jsonl(path, rows):
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def _stt(index):
    return {
        "schema_version": 2,
        "event_type": "stt",
        "run_id": "run-a",
        "utterance_id": f"utt-{index}",
        "engine": "groq",
        "model": "whisper-large-v3",
        "status": "success",
        "avg_logprob": -0.2,
        "no_speech_prob": 0.01,
        "audio_seconds": 1.0,
    }


def _translation(index, **overrides):
    row = {
        "schema_version": 2,
        "event_type": "translation",
        "run_id": "run-a",
        "sequence_id": index,
        "utterance_id": f"utt-{index}",
        "source_utterance_ids": [f"utt-{index}"],
        "source_text": f"source {index}",
        "target_text": f"target {index}",
        "status": "success",
        "cut_reason": "natural",
        "quality_flags": [],
        "source_avg_logprobs": [-0.2],
        "source_no_speech_probs": [0.01],
        "min_avg_logprob": -0.2,
        "max_no_speech_prob": 0.01,
    }
    row.update(overrides)
    return row


def _touch_wavs(audio_root, utterance_ids):
    run_dir = audio_root / "run-a"
    run_dir.mkdir(parents=True, exist_ok=True)
    for utterance_id in utterance_ids:
        (run_dir / f"{utterance_id}.wav").write_bytes(b"RIFF")


def test_build_phase0_candidates_uses_host_primary_rules_and_buckets(tmp_path):
    events_path = tmp_path / "runtime_events_20260613.jsonl"
    audio_root = tmp_path / "audio_dump"
    rows = [
        _stt(1),
        _stt(2),
        _stt(3),
        _translation(1, cut_reason="forced_blob"),
        _translation(2, cut_reason="silence_complete"),
        _translation(
            3,
            quality_flags=["target_has_hangul"],
            source_avg_logprobs=[-0.8],
            min_avg_logprob=-0.8,
        ),
    ]
    _write_jsonl(events_path, rows)
    _touch_wavs(audio_root, ["utt-1", "utt-2", "utt-3"])

    sample = build_phase0_candidates(
        events_path=events_path,
        audio_root=audio_root,
        seed=1,
        total=3,
        random_count=0,
        forced_count=1,
        silence_count=1,
        multi_count=0,
        low_confidence_count=0,
        suspicious_count=1,
    )

    assert sample["speaker_policy"] == "host-primary"
    assert "host_only" in sample["speaker_source_options"]
    assert any("Host-primary" in rule for rule in sample["annotation_rules"])
    assert sample["sampling"]["eligible_population_size"] == 3
    assert sample["sampling"]["bucket_counts"]["forced_cut"] == 1
    assert sample["sampling"]["bucket_counts"]["silence_complete"] == 1
    assert sample["sampling"]["bucket_counts"]["quality_suspicious"] == 1
    assert {item["phase0_bucket"] for item in sample["samples"]} == {
        "forced_cut",
        "silence_complete",
        "quality_suspicious",
    }
    assert sample["quality_control"]["missing_audio_files"] == []
    assert sample["quality_control"]["missing_stt_events"] == []


@pytest.mark.parametrize(
    ("total", "random_count", "forced_count", "message"),
    [
        (2, 2, 1, "must not exceed total"),
        (2, -1, 0, "must be non-negative"),
    ],
)
def test_build_phase0_candidates_rejects_invalid_bucket_totals(
    tmp_path, total, random_count, forced_count, message
):
    with pytest.raises(ValueError, match=message):
        build_phase0_candidates(
            events_path=tmp_path / "unused.jsonl",
            total=total,
            random_count=random_count,
            forced_count=forced_count,
            silence_count=0,
            multi_count=0,
            low_confidence_count=0,
            suspicious_count=0,
        )
