import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from scripts.evaluate_weak_signal_dual_asr import (
    analyze_dual_replay,
    build_manifest,
)


def _write_wav(path: Path, seconds: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, np.zeros(round(seconds * 16000), dtype=np.float32), 16000, subtype="PCM_16")


def _stt(run_id: str, utterance_id: str, *, logprob: float, seconds: float, rms: float = 0.004):
    return {
        "schema_version": 3,
        "event_type": "stt",
        "run_id": run_id,
        "run_kind": "live",
        "created_at": f"2026-08-02T00:00:{int(utterance_id.split('-')[-1]):02d}+00:00",
        "utterance_id": utterance_id,
        "engine": "groq",
        "model": "whisper-large-v3",
        "status": "success",
        "request_sent": True,
        "text_len": 2,
        "audio_seconds": seconds,
        "avg_logprob": logprob,
        "no_speech_prob": 0.1,
        "audio_rms": rms,
        "overlap_seconds": 0.0,
        "vad_cut_reason": "silence",
        "context_included": True,
        "profile_id": "profile-a",
    }


def _translation(run_id: str, utterance_id: str):
    return {
        "event_type": "translation",
        "run_id": run_id,
        "status": "success",
        "source_count": 1,
        "source_utterance_ids": [utterance_id],
        "evidence_source_utterance_ids": [],
        "source_text": "가나",
    }


def _write_events(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _provenance(path: Path, run_id: str, annotated_utterance: str = "") -> None:
    audio_refs = [] if not annotated_utterance else [{"utterance_id": annotated_utterance}]
    path.write_text(
        json.dumps(
            {
                "run_summaries": [{"run_id": run_id}],
                "annotations": [
                    {
                        "timestamp_matches": [
                            {"runtime_refs": [{"run_id": run_id, "audio_refs": audio_refs}]}
                        ]
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_build_manifest_selects_and_matches_without_using_annotated_control(tmp_path):
    run_id = "run-a"
    audio_root = tmp_path / "audio"
    events_path = tmp_path / "events.jsonl"
    provenance_path = tmp_path / "provenance.json"
    rows = [
        _stt(run_id, "utt-1", logprob=-0.8, seconds=3.0),
        _translation(run_id, "utt-1"),
        _stt(run_id, "utt-2", logprob=-0.1, seconds=3.0),
        _translation(run_id, "utt-2"),
        _stt(run_id, "utt-3", logprob=-0.2, seconds=3.1),
        _translation(run_id, "utt-3"),
    ]
    for utterance_id, seconds in (("utt-1", 3.0), ("utt-2", 3.0), ("utt-3", 3.1)):
        _write_wav(audio_root / run_id / f"{utterance_id}.wav", seconds)
    _write_events(events_path, rows)
    _provenance(provenance_path, run_id, annotated_utterance="utt-2")

    manifest = build_manifest(
        events_path=events_path,
        provenance_path=provenance_path,
        audio_root=audio_root,
        project_root=tmp_path,
    )

    assert manifest["population_count"] == 3
    assert manifest["weak_count"] == 1
    assert manifest["matches"][0]["weak_key"] == [run_id, "utt-1"]
    assert manifest["matches"][0]["control_key"] == [run_id, "utt-3"]
    assert {case["cohort"] for case in manifest["cases"]} == {"weak_signal", "matched_control"}


def test_build_manifest_fails_closed_outside_match_caliper(tmp_path):
    run_id = "run-a"
    audio_root = tmp_path / "audio"
    events_path = tmp_path / "events.jsonl"
    provenance_path = tmp_path / "provenance.json"
    rows = [
        _stt(run_id, "utt-1", logprob=-0.8, seconds=3.0),
        _translation(run_id, "utt-1"),
        _stt(run_id, "utt-2", logprob=-0.1, seconds=6.0),
        _translation(run_id, "utt-2"),
    ]
    for utterance_id, seconds in (("utt-1", 3.0), ("utt-2", 6.0)):
        _write_wav(audio_root / run_id / f"{utterance_id}.wav", seconds)
    _write_events(events_path, rows)
    _provenance(provenance_path, run_id)

    with pytest.raises(ValueError, match="no matched control"):
        build_manifest(
            events_path=events_path,
            provenance_path=provenance_path,
            audio_root=audio_root,
            project_root=tmp_path,
        )


def test_analyze_dual_replay_freezes_empty_denominator_and_no_go(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest = {
        "cases": [
            {
                "sample_id": "weak",
                "pair_id": "pair-001",
                "cohort": "weak_signal",
                "groq_comparison_eligible": True,
                "sample": {"run_id": "run-a", "source_utterance_ids": ["utt-1"], "source_text": "시청자"},
            },
            {
                "sample_id": "control",
                "pair_id": "pair-001",
                "cohort": "matched_control",
                "groq_comparison_eligible": False,
                "sample": {"run_id": "run-a", "source_utterance_ids": ["utt-2"], "source_text": ""},
            },
        ]
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()

    def result(engine: str, texts: dict[str, str]):
        return {
            "manifest_sha256": manifest_sha,
            "engine": engine,
            "cases": [
                {"sample_id": sample_id, "candidate_current_text": text}
                for sample_id, text in texts.items()
            ],
        }

    sensevoice_path = tmp_path / "sensevoice.json"
    faster_path = tmp_path / "faster.json"
    sensevoice_path.write_text(
        json.dumps(result("sensevoice", {"weak": "스토리", "control": ""})), encoding="utf-8"
    )
    faster_path.write_text(
        json.dumps(result("faster_whisper", {"weak": "스토리", "control": "디" * 20})), encoding="utf-8"
    )

    analysis = analyze_dual_replay(
        manifest_path=manifest_path,
        sensevoice_path=sensevoice_path,
        faster_whisper_path=faster_path,
    )

    assert analysis["summaries"]["weak_signal"]["local_consensus_disagreement_count"] == 1
    assert analysis["summaries"]["weak_signal"]["local_consensus_disagreement_denominator"] == 1
    assert analysis["summaries"]["weak_signal"]["local_similarity_median"] == 1.0
    assert analysis["summaries"]["matched_control"]["local_consensus_disagreement_denominator"] == 0
    assert analysis["engine_performance"]["sensevoice"]["result_sha256"]
    control = next(row for row in analysis["cases"] if row["sample_id"] == "control")
    assert control["faster_whisper_proxy_flags"] == ["low_distinct_bigram_repetition"]
    assert analysis["review_priority"] == ["weak"]
    assert analysis["live_shadow_decision"] == "no-go"
