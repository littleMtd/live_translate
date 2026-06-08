import json

from scripts.collection_sanity_report import build_collection_sanity_report


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
        "profile_id": "mwmeu",
        "cut_reason": "natural",
    }
    base.update(overrides)
    source_ids = base.get("source_utterance_ids", [])
    if "source_count" not in overrides:
        base["source_count"] = len(source_ids)
    if "source_avg_logprobs" not in overrides:
        base["source_avg_logprobs"] = [-0.25 for _ in source_ids]
    if "source_no_speech_probs" not in overrides:
        base["source_no_speech_probs"] = [0.05 for _ in source_ids]
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
        "created_at": f"2026-05-31T00:00:{index:02d}+00:00",
        "utterance_id": f"utt-{index}",
        "status": "success",
        "engine": "groq",
        "model": "whisper-large-v3",
        "request_sent": True,
        "audio_seconds": 1.5,
        "avg_logprob": -0.25,
        "no_speech_prob": 0.05,
    }
    base.update(overrides)
    return base


def _audio_event(index: int, **overrides):
    base = {
        "schema_version": 2,
        "event_type": "audio",
        "run_id": "run-a",
        "created_at": f"2026-05-31T00:00:{index:02d}+00:00",
        "audio_seconds": 1.5,
        "cut_reason": "silence",
    }
    base.update(overrides)
    return base


def _touch_wavs(audio_root, run_id, utterance_ids):
    run_dir = audio_root / run_id
    run_dir.mkdir(parents=True)
    for utterance_id in utterance_ids:
        (run_dir / f"{utterance_id}.wav").write_bytes(b"RIFF")


def test_report_returns_unavailable_for_missing_file(tmp_path):
    report = build_collection_sanity_report(events_path=tmp_path / "missing.jsonl")

    assert report["available"] is False


def test_report_filters_schema2_translation_events_and_run_id(tmp_path):
    events_path = tmp_path / "runtime_events_20260531.jsonl"
    audio_root = tmp_path / "audio_dump"
    rows = [
        _translation_event(1, run_id="run-a"),
        _translation_event(2, run_id="run-b"),
        _translation_event(3, schema_version=1, run_id="run-a"),
        _stt_event(1, run_id="run-a"),
        _stt_event(2, run_id="run-b"),
        _audio_event(1, run_id="run-a"),
    ]
    _write_jsonl(events_path, rows)
    _touch_wavs(audio_root, "run-a", ["utt-1"])
    _touch_wavs(audio_root, "run-b", ["utt-2"])

    report = build_collection_sanity_report(
        events_path=events_path,
        audio_root=audio_root,
        run_ids={"run-a"},
        min_population=1,
    )

    assert report["available"] is True
    assert report["counts"]["translation_events"] == 1
    assert report["counts"]["stt_events"] == 1
    assert report["profiles"] == [{"value": "mwmeu", "count": 1}]
    assert report["ready_for_sampling"] is True
    assert report["run_summaries"][0]["run_id"] == "run-a"
    assert report["run_summaries"][0]["wav_files"] == 1


def test_report_join_quality_counts_multi_chunk_duplicates_and_gaps(tmp_path):
    events_path = tmp_path / "runtime_events_20260531.jsonl"
    audio_root = tmp_path / "audio_dump"
    rows = [
        _stt_event(1),
        _stt_event(2, avg_logprob=None),
        _translation_event(1, source_utterance_ids=["utt-1", "utt-2", "utt-1"]),
        _translation_event(3, source_utterance_ids=["utt-3"]),
        _translation_event(4, source_utterance_ids=[]),
    ]
    _write_jsonl(events_path, rows)
    _touch_wavs(audio_root, "run-a", ["utt-1"])

    report = build_collection_sanity_report(
        events_path=events_path,
        audio_root=audio_root,
        min_population=0,
    )
    join = report["join_quality"]

    assert join["translation_events"] == 3
    assert join["translations_with_source_ids"] == 2
    assert join["source_id_refs"] == 4
    assert join["unique_source_id_refs"] == 3
    assert join["duplicate_source_id_refs"] == 1
    assert join["translations_with_duplicate_source_ids"] == 1
    assert join["multi_chunk_translations"] == 1
    assert join["missing_source_id_translations"] == 1
    assert join["missing_stt_event_refs"] == 1
    assert join["missing_audio_file_refs"] == 2
    assert join["missing_confidence_refs"] == 1
    assert join["source_confidence_diagnostic_issues"] == 0
    assert join["audio_join_ok_translations"] == 0
    assert report["ready_for_sampling"] is False


def test_report_allows_evidence_only_source_for_replay_join(tmp_path):
    events_path = tmp_path / "runtime_events_20260531.jsonl"
    audio_root = tmp_path / "audio_dump"
    rows = [
        _stt_event(1),
        _translation_event(
            1,
            source_utterance_ids=[],
            evidence_source_utterance_ids=["utt-1"],
            source_count=0,
            source_avg_logprobs=[],
            source_no_speech_probs=[],
            min_avg_logprob=None,
            max_no_speech_prob=None,
        ),
    ]
    _write_jsonl(events_path, rows)
    _touch_wavs(audio_root, "run-a", ["utt-1"])

    report = build_collection_sanity_report(
        events_path=events_path,
        audio_root=audio_root,
        min_population=0,
    )
    join = report["join_quality"]

    assert join["translations_with_source_ids"] == 1
    assert join["missing_source_id_translations"] == 0
    assert join["source_id_refs"] == 0
    assert join["evidence_source_id_refs"] == 1
    assert join["audio_join_ok_translations"] == 1
    assert report["ready_for_sampling"] is True


def test_report_flags_source_confidence_diagnostic_alignment_gaps(tmp_path):
    events_path = tmp_path / "runtime_events_20260531.jsonl"
    audio_root = tmp_path / "audio_dump"
    rows = [
        _stt_event(1),
        _stt_event(2),
        _translation_event(
            1,
            source_utterance_ids=["utt-1", "utt-2"],
            source_count=1,
            source_avg_logprobs=[-0.25],
            source_no_speech_probs=[0.05, None],
            min_avg_logprob=-0.25,
            max_no_speech_prob=0.8,
        ),
    ]
    _write_jsonl(events_path, rows)
    _touch_wavs(audio_root, "run-a", ["utt-1", "utt-2"])

    report = build_collection_sanity_report(
        events_path=events_path,
        audio_root=audio_root,
        min_population=0,
    )
    join = report["join_quality"]

    assert report["ready_for_sampling"] is False
    assert join["source_confidence_diagnostic_issues"] == 3
    issue_counts = {item["value"]: item["count"] for item in join["source_confidence_issue_counts"]}
    assert issue_counts["source_count_mismatch"] == 1
    assert issue_counts["source_avg_logprobs_length_mismatch"] == 1
    assert issue_counts["max_no_speech_prob_mismatch"] == 1


def test_report_marks_mixed_profiles_not_ready(tmp_path):
    events_path = tmp_path / "runtime_events_20260531.jsonl"
    audio_root = tmp_path / "audio_dump"
    rows = [
        _stt_event(1),
        _stt_event(2),
        _translation_event(1, profile_id="mwmeu"),
        _translation_event(2, profile_id="stellive_hina"),
    ]
    _write_jsonl(events_path, rows)
    _touch_wavs(audio_root, "run-a", ["utt-1", "utt-2"])

    report = build_collection_sanity_report(
        events_path=events_path,
        audio_root=audio_root,
        min_population=2,
    )

    assert report["ready_for_sampling"] is False
    assert {item["value"] for item in report["profiles"]} == {"mwmeu", "stellive_hina"}
    assert any("Multiple profile_id" in item["message"] for item in report["recommendations"])


def test_report_flags_suspicious_text_patterns(tmp_path):
    events_path = tmp_path / "runtime_events_20260531.jsonl"
    audio_root = tmp_path / "audio_dump"
    echo_text = (
        "2, 오마쿡스, 땡글즈, 띠빵뽕, 치이카와, 하치와레, 모몽가, 우사기, 마인크래프트, Minecraft. "
        "3, 오마쿡스, 땡글즈, 띠빵뽕, 치이카와, 하치와레, 모몽가, 우사기, 마인크래프트, Minecraft."
    )
    rows = [
        _stt_event(1),
        _stt_event(2),
        _stt_event(3),
        _stt_event(4),
        _translation_event(1, source_text=echo_text),
        _translation_event(2, source_text="repeat me"),
        _translation_event(3, source_text="repeat me"),
        _translation_event(4, source_text="x" * 30, target_text=""),
    ]
    _write_jsonl(events_path, rows)
    _touch_wavs(audio_root, "run-a", ["utt-1", "utt-2", "utt-3", "utt-4"])

    report = build_collection_sanity_report(
        events_path=events_path,
        audio_root=audio_root,
        min_population=0,
        long_text_chars=20,
    )
    suspicious = report["suspicious"]

    assert suspicious["glossary_echo_candidates"]["count"] == 1
    assert suspicious["repeated_source_text"]["duplicate_event_count"] == 1
    assert suspicious["empty_target_text"]["count"] == 1
    assert suspicious["very_long_source_text"]["count"] == 2
    assert any("Glossary/prompt echo" in item["message"] for item in report["recommendations"])
