import json
import random

import pytest

from scripts.sample_labeling_cases import (
    build_labeling_sample,
    build_stt_index,
    read_runtime_rows,
    translation_population,
)


def _write_jsonl(path, rows):
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def _translation_event(index: int, **overrides):
    base = {
        "schema_version": 2,
        "event_type": "translation",
        "run_id": "run-a",
        "created_at": f"2026-05-31T00:00:{index:02d}+00:00",
        "sequence_id": index,
        "utterance_id": f"utt-{index}",
        "source_utterance_ids": [f"utt-{index}"],
        "source_text": f"source {index}",
        "target_text": f"target {index}",
        "status": "success",
        "engine": "nvidia",
        "quality_flags": [],
        "cut_reason": "natural",
    }
    base.update(overrides)
    source_ids = base.get("source_utterance_ids", [])
    if "source_count" not in overrides:
        base["source_count"] = len(source_ids)
    if "source_avg_logprobs" not in overrides:
        base["source_avg_logprobs"] = [-0.25 for _ in source_ids]
    if "source_no_speech_probs" not in overrides:
        base["source_no_speech_probs"] = [0.1 for _ in source_ids]
    if "min_avg_logprob" not in overrides:
        avg_values = [value for value in base["source_avg_logprobs"] if value is not None]
        base["min_avg_logprob"] = min(avg_values) if avg_values else None
    if "max_no_speech_prob" not in overrides:
        no_speech_values = [value for value in base["source_no_speech_probs"] if value is not None]
        base["max_no_speech_prob"] = max(no_speech_values) if no_speech_values else None
    return base


def _stt_event(index: int, **overrides):
    base = {
        "schema_version": 2,
        "event_type": "stt",
        "run_id": "run-a",
        "utterance_id": f"utt-{index}",
        "engine": "groq",
        "model": "whisper-large-v3",
        "status": "success",
        "reason": "",
        "request_sent": True,
        "audio_seconds": 1.25,
        "latency_ms": 250,
        "text_len": 12,
        "avg_logprob": -0.25,
        "no_speech_prob": 0.1,
    }
    base.update(overrides)
    return base


def _sentence_event(index: int, **overrides):
    base = {
        "schema_version": 2,
        "event_type": "sentence",
        "run_id": "run-a",
        "utterance_id": f"utt-{index}",
        "source_utterance_ids": [f"utt-{index}"],
        "cut_reason": "natural",
        "forced": False,
        "chunk_count": 1,
        "audio_seconds": 1.25,
    }
    base.update(overrides)
    return base


def _audio_event(index: int, **overrides):
    base = {
        "schema_version": 2,
        "event_type": "audio",
        "run_id": "run-a",
        "audio_seconds": 1.25,
        "raw_audio_seconds": 1.0,
        "overlap_seconds": 0.25,
        "cut_reason": "silence",
        "adaptive_active": False,
    }
    base.update(overrides)
    return base


def _touch_wavs(audio_root, run_id, utterance_ids):
    run_dir = audio_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    for utterance_id in utterance_ids:
        (run_dir / f"{utterance_id}.wav").write_bytes(b"RIFF")


def test_population_uses_only_schema2_translation_events(tmp_path):
    events_path = tmp_path / "runtime_events_20260531.jsonl"
    rows = [
        _translation_event(1),
        _translation_event(2, schema_version=1),
        _stt_event(1),
        {"schema_version": 2, "event_type": "sentence", "run_id": "run-a"},
        _translation_event(3),
    ]
    _write_jsonl(events_path, rows)

    runtime_rows = read_runtime_rows(events_path)
    population = translation_population(runtime_rows)

    assert [row["event"]["sequence_id"] for row in population] == [1, 3]


def test_population_can_be_restricted_to_exact_run_id(tmp_path):
    events_path = tmp_path / "runtime_events_20260531.jsonl"
    rows = [
        _translation_event(1, run_id="run-a"),
        _translation_event(2, run_id="run-b"),
        _translation_event(3, run_id="run-a"),
    ]
    _write_jsonl(events_path, rows)

    runtime_rows = read_runtime_rows(events_path)
    population = translation_population(runtime_rows, {"run-b"})

    assert [row["event"]["sequence_id"] for row in population] == [2]


def test_build_labeling_sample_uniform_random_and_joins_audio_and_confidence(tmp_path):
    events_path = tmp_path / "runtime_events_20260531.jsonl"
    audio_root = tmp_path / "audio_dump"
    translations = [_translation_event(index) for index in range(1, 7)]
    rows = [_stt_event(index) for index in range(1, 7)] + translations
    _write_jsonl(events_path, rows)
    _touch_wavs(audio_root, "run-a", [f"utt-{index}" for index in range(1, 7)])

    sample = build_labeling_sample(
        events_path=events_path,
        audio_root=audio_root,
        sample_size=3,
        seed=123,
        min_population=0,
    )

    expected = random.Random(123).sample(list(range(1, 7)), 3)
    assert sample["sampling"]["method"] == "uniform_random_without_replacement"
    assert sample["speaker_policy"] == "host-primary"
    assert "host_only" in sample["speaker_source_options"]
    assert any("Host-primary" in rule for rule in sample["annotation_rules"])
    assert sample["sampling"]["raw_population_size"] == 6
    assert sample["sampling"]["excluded_missing_source_id_population"] == 0
    assert sample["sampling"]["population_size"] == 6
    assert "over_attributed_chunks" in sample["context_tag_options"]
    assert [item["sequence_id"] for item in sample["samples"]] == expected
    first_chunk = sample["samples"][0]["source_chunks"][0]
    assert first_chunk["chunk_role"] == "primary"
    assert first_chunk["audio_exists"] is True
    assert first_chunk["stt_event_found"] is True
    assert first_chunk["avg_logprob"] == -0.25
    assert first_chunk["no_speech_prob"] == 0.1
    assert first_chunk["audio_path"].endswith(f"run-a\\utt-{expected[0]}.wav") or first_chunk["audio_path"].endswith(f"run-a/utt-{expected[0]}.wav")


def test_build_labeling_sample_records_run_id_filter(tmp_path):
    events_path = tmp_path / "runtime_events_20260531.jsonl"
    audio_root = tmp_path / "audio_dump"
    rows = [
        _stt_event(1, run_id="run-a"),
        _stt_event(2, run_id="run-b"),
        _translation_event(1, run_id="run-a"),
        _translation_event(2, run_id="run-b"),
    ]
    _write_jsonl(events_path, rows)
    _touch_wavs(audio_root, "run-a", ["utt-1"])
    _touch_wavs(audio_root, "run-b", ["utt-2"])

    sample = build_labeling_sample(
        events_path=events_path,
        audio_root=audio_root,
        sample_size=1,
        seed=1,
        min_population=0,
        run_ids={"run-b"},
    )

    assert sample["sampling"]["run_ids"] == ["run-b"]
    assert sample["samples"][0]["run_id"] == "run-b"


def test_sample_preserves_multi_chunk_evidence_with_unique_chunk_rows(tmp_path):
    events_path = tmp_path / "runtime_events_20260531.jsonl"
    audio_root = tmp_path / "audio_dump"
    rows = [
        _stt_event(1),
        _stt_event(2),
        _translation_event(1, source_utterance_ids=["utt-1", "utt-2", "utt-1"]),
    ]
    _write_jsonl(events_path, rows)
    _touch_wavs(audio_root, "run-a", ["utt-1", "utt-2"])

    sample = build_labeling_sample(
        events_path=events_path,
        audio_root=audio_root,
        sample_size=1,
        seed=1,
        min_population=0,
    )

    item = sample["samples"][0]
    assert item["source_utterance_ids"] == ["utt-1", "utt-2", "utt-1"]
    assert item["unique_source_utterance_ids"] == ["utt-1", "utt-2"]
    assert [chunk["utterance_id"] for chunk in item["source_chunks"]] == ["utt-1", "utt-2"]
    assert [chunk["chunk_role"] for chunk in item["source_chunks"]] == ["primary", "supporting"]
    assert [usage["utterance_id"] for usage in item["source_chunk_usages"]] == ["utt-1", "utt-2", "utt-1"]
    assert [usage["current_translation_source_index"] for usage in item["source_chunk_usages"]] == [0, 1, 2]
    assert [usage["role"] for usage in item["source_chunk_usages"]] == ["primary", "supporting", "primary"]


def test_sample_marks_prior_overlap_chunks_without_changing_random_sampling(tmp_path):
    events_path = tmp_path / "runtime_events_20260531.jsonl"
    audio_root = tmp_path / "audio_dump"
    rows = [
        _stt_event(1),
        _stt_event(2),
        _translation_event(
            1,
            source_utterance_ids=["utt-1"],
            source_text="earlier source",
            cut_reason="forced_prefix",
        ),
        _translation_event(
            2,
            utterance_id="utt-2",
            source_utterance_ids=["utt-1", "utt-2"],
            source_text="later source",
            cut_reason="merged:forced_prefix+natural",
        ),
    ]
    _write_jsonl(events_path, rows)
    _touch_wavs(audio_root, "run-a", ["utt-1", "utt-2"])

    sample = build_labeling_sample(
        events_path=events_path,
        audio_root=audio_root,
        sample_size=1,
        seed=0,
        min_population=0,
    )

    item = sample["samples"][0]
    assert item["sequence_id"] == 2
    assert item["source_overlap_warning"] is True
    assert item["prior_overlap_utterance_ids"] == ["utt-1"]
    assert item["chunk_role_counts"] == {"primary": 1, "prior_overlap": 1, "supporting": 0}
    assert [chunk["chunk_role"] for chunk in item["source_chunks"]] == ["prior_overlap", "primary"]
    overlap_chunk = item["source_chunks"][0]
    assert overlap_chunk["prior_translation_sequence_ids"] == [1]
    assert overlap_chunk["prior_translation_source_texts"] == ["earlier source"]
    usage = item["source_chunk_usages"][0]
    assert usage["utterance_id"] == "utt-1"
    assert usage["translation_cut_reason"] == "merged:forced_prefix+natural"
    assert usage["role"] == "prior_overlap"
    assert usage["role_reason"] == "source_utterance_seen_in_prior_translation"
    assert usage["first_seen_translation_index"] == 1
    assert usage["prior_translation_indices"] == [1]
    assert usage["prior_translation_cut_reasons"] == ["forced_prefix"]
    assert usage["current_translation_source_index"] == 0


def test_sample_uses_explicit_evidence_source_for_prior_overlap(tmp_path):
    events_path = tmp_path / "runtime_events_20260531.jsonl"
    audio_root = tmp_path / "audio_dump"
    rows = [
        _stt_event(1),
        _stt_event(2),
        _translation_event(1, source_utterance_ids=["utt-1"], source_text="earlier source"),
        _translation_event(
            2,
            utterance_id="utt-2",
            source_utterance_ids=["utt-2"],
            evidence_source_utterance_ids=["utt-1"],
            source_text="later source",
            cut_reason="natural",
        ),
    ]
    _write_jsonl(events_path, rows)
    _touch_wavs(audio_root, "run-a", ["utt-1", "utt-2"])

    sample = build_labeling_sample(
        events_path=events_path,
        audio_root=audio_root,
        sample_size=1,
        seed=0,
        min_population=0,
    )

    item = sample["samples"][0]
    assert item["sequence_id"] == 2
    assert item["source_utterance_ids"] == ["utt-2"]
    assert item["evidence_source_utterance_ids"] == ["utt-1"]
    assert item["prior_overlap_utterance_ids"] == ["utt-1"]
    assert [chunk["utterance_id"] for chunk in item["source_chunks"]] == ["utt-2", "utt-1"]
    assert [chunk["source_kind"] for chunk in item["source_chunks"]] == ["current", "evidence"]
    assert [chunk["chunk_role"] for chunk in item["source_chunks"]] == ["primary", "prior_overlap"]
    assert item["chunk_role_counts"] == {"primary": 1, "prior_overlap": 1, "supporting": 0}
    assert item["source_chunk_usages"][0]["source_kind"] == "current"
    assert item["source_chunk_usages"][1]["source_kind"] == "evidence"
    assert item["source_chunk_usages"][1]["evidence_source_index"] == 0
    assert item["source_chunk_usages"][1]["current_translation_source_index"] is None


def test_sample_allows_evidence_only_current_source_empty(tmp_path):
    events_path = tmp_path / "runtime_events_20260531.jsonl"
    audio_root = tmp_path / "audio_dump"
    rows = [
        _stt_event(1),
        _translation_event(
            1,
            utterance_id="",
            source_utterance_ids=[],
            evidence_source_utterance_ids=["utt-1"],
            source_count=0,
            source_avg_logprobs=[],
            source_no_speech_probs=[],
            min_avg_logprob=None,
            max_no_speech_prob=None,
            cut_reason="forced_blob",
        ),
    ]
    _write_jsonl(events_path, rows)
    _touch_wavs(audio_root, "run-a", ["utt-1"])

    sample = build_labeling_sample(
        events_path=events_path,
        audio_root=audio_root,
        sample_size=1,
        seed=1,
        min_population=0,
    )

    item = sample["samples"][0]
    assert item["source_utterance_ids"] == []
    assert item["evidence_source_utterance_ids"] == ["utt-1"]
    assert item["source_chunks"][0]["source_kind"] == "evidence"
    assert sample["quality_control"]["missing_source_id_samples"] == []


def test_sample_joins_nearest_sentence_and_audio_context_without_chunk_cut_reason(tmp_path):
    events_path = tmp_path / "runtime_events_20260531.jsonl"
    audio_root = tmp_path / "audio_dump"
    rows = [
        _audio_event(1, cut_reason="hard_max", audio_seconds=3.0),
        _stt_event(1),
        _sentence_event(
            1,
            cut_reason="forced_prefix",
            forced=True,
            chunk_count=1,
            audio_seconds=3.0,
        ),
        _translation_event(1, cut_reason=""),
    ]
    _write_jsonl(events_path, rows)
    _touch_wavs(audio_root, "run-a", ["utt-1"])

    sample = build_labeling_sample(
        events_path=events_path,
        audio_root=audio_root,
        sample_size=1,
        seed=1,
        min_population=0,
    )

    item = sample["samples"][0]
    assert item["translation_event_id"] == "run-a:line:4"
    assert item["translation_index"] == 1
    assert item["nearest_sentence_event_line"] == 3
    assert item["sentence_cut_reason"] == "forced_prefix"
    assert item["translation_cut_reason"] == "forced_prefix"
    assert item["sentence_source_utterance_ids"] == ["utt-1"]
    chunk = item["source_chunks"][0]
    assert "cut_reason" not in chunk
    assert chunk["audio_context"]["line_no"] == 1
    assert chunk["audio_context"]["vad_cut_reason"] == "hard_max"


def test_missing_audio_fails_by_default_without_resampling(tmp_path):
    events_path = tmp_path / "runtime_events_20260531.jsonl"
    rows = [_stt_event(1), _translation_event(1)]
    _write_jsonl(events_path, rows)

    with pytest.raises(ValueError, match="missing_audio_files=1"):
        build_labeling_sample(
            events_path=events_path,
            audio_root=tmp_path / "audio_dump",
            sample_size=1,
            seed=1,
            min_population=0,
        )


def test_missing_source_utterance_ids_excluded_before_sampling_by_default(tmp_path):
    events_path = tmp_path / "runtime_events_20260531.jsonl"
    rows = [
        _stt_event(1),
        _translation_event(1),
        _translation_event(2, source_utterance_ids=[]),
    ]
    _write_jsonl(events_path, rows)
    _touch_wavs(tmp_path / "audio_dump", "run-a", ["utt-1"])

    sample = build_labeling_sample(
        events_path=events_path,
        audio_root=tmp_path / "audio_dump",
        sample_size=1,
        seed=1,
        min_population=0,
    )

    assert sample["sampling"]["raw_population_size"] == 2
    assert sample["sampling"]["excluded_missing_source_id_population"] == 1
    assert sample["sampling"]["population_size"] == 1
    assert sample["samples"][0]["sequence_id"] == 1
    assert sample["quality_control"]["missing_source_id_samples"] == []


def test_missing_source_utterance_ids_reported_with_allow_missing_audio(tmp_path):
    events_path = tmp_path / "runtime_events_20260531.jsonl"
    rows = [_translation_event(1, source_utterance_ids=[])]
    _write_jsonl(events_path, rows)

    sample = build_labeling_sample(
        events_path=events_path,
        audio_root=tmp_path / "audio_dump",
        sample_size=1,
        seed=1,
        min_population=0,
        allow_missing_audio=True,
    )

    assert sample["sampling"]["raw_population_size"] == 1
    assert sample["sampling"]["excluded_missing_source_id_population"] == 0
    assert sample["quality_control"]["missing_source_id_samples"] == ["S001"]


def test_missing_stt_event_fails_by_default(tmp_path):
    events_path = tmp_path / "runtime_events_20260531.jsonl"
    audio_root = tmp_path / "audio_dump"
    rows = [_translation_event(1)]
    _write_jsonl(events_path, rows)
    _touch_wavs(audio_root, "run-a", ["utt-1"])

    with pytest.raises(ValueError, match="missing_stt_events=1"):
        build_labeling_sample(
            events_path=events_path,
            audio_root=audio_root,
            sample_size=1,
            seed=1,
            min_population=0,
        )


def test_allow_missing_audio_reports_quality_control(tmp_path):
    events_path = tmp_path / "runtime_events_20260531.jsonl"
    rows = [_translation_event(1, source_utterance_ids=[])]
    _write_jsonl(events_path, rows)

    sample = build_labeling_sample(
        events_path=events_path,
        audio_root=tmp_path / "audio_dump",
        sample_size=1,
        seed=1,
        min_population=0,
        allow_missing_audio=True,
    )

    assert sample["quality_control"]["missing_source_id_samples"] == ["S001"]


def test_build_stt_index_ignores_non_schema2_rows(tmp_path):
    events_path = tmp_path / "runtime_events_20260531.jsonl"
    rows = [_stt_event(1), _stt_event(2, schema_version=1)]
    _write_jsonl(events_path, rows)

    index = build_stt_index(read_runtime_rows(events_path))

    assert ("run-a", "utt-1") in index
    assert ("run-a", "utt-2") not in index
