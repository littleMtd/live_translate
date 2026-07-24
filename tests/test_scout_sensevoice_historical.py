import numpy as np
import soundfile as sf

from scripts.scout_sensevoice_historical import build_report, run_scout, select_candidates


def test_select_candidates_requires_exact_single_utterance_audio(tmp_path):
    audio_root = tmp_path / "audio_dump"
    path = audio_root / "run-a" / "utt-1.wav"
    path.parent.mkdir(parents=True)
    sf.write(path, np.zeros(1600, dtype=np.float32), 16000)
    translation = {
        "event_type": "translation",
        "run_id": "run-a",
        "created_at": "2026-07-11T00:00:00Z",
        "status": "success",
        "incomplete": False,
        "source_count": 1,
        "source_utterance_ids": ["utt-1"],
        "evidence_source_utterance_ids": [],
        "source_text": "안녕하세요",
        "quality_severity": "warn",
        "avg_logprob": -0.7,
    }
    stt = {
        "event_type": "stt",
        "run_id": "run-a",
        "utterance_id": "utt-1",
        "created_at": "2026-07-11T00:00:00Z",
        "status": "success",
        "request_sent": True,
        "text_len": len(translation["source_text"]),
        "avg_logprob": -0.7,
    }

    selected = select_candidates(
        [
            stt,
            translation,
            {**translation, "source_utterance_ids": ["utt-1", "utt-2"]},
        ],
        audio_root=audio_root,
        limit=10,
    )

    assert len(selected) == 1
    assert selected[0]["utterance_id"] == "utt-1"
    assert selected[0]["groq_text_alignment"] == "single_source_length_match"
    assert selected[0]["trigger_reasons"] == ["low_logprob"]


def test_scout_reports_disagreement_without_claiming_correctness(tmp_path):
    path = tmp_path / "audio.wav"
    sf.write(path, np.zeros(1600, dtype=np.float32), 16000)
    candidates = [
        {
            "audio_path": "audio.wav",
            "groq_text": "안녕하세요",
            "run_id": "run-a",
            "utterance_id": "utt-1",
        }
    ]

    results = run_scout(candidates, generate=lambda _audio: "<|ko|>다른 말", project_root=tmp_path)
    report = build_report(candidates, results)

    assert results[0]["sensevoice_text"] == "다른 말"
    assert report["ground_truth_count"] == 0
    assert report["measured_rescues"] is None
    assert report["live_shadow_decision"] == "no-go"


def test_filtered_compression_candidate_uses_audio_without_inventing_groq_text(tmp_path):
    audio_root = tmp_path / "audio_dump"
    path = audio_root / "run-a" / "utt-2.wav"
    path.parent.mkdir(parents=True)
    sf.write(path, np.zeros(1600, dtype=np.float32), 16000)
    stt = {
        "event_type": "stt",
        "run_id": "run-a",
        "utterance_id": "utt-2",
        "created_at": "2026-07-11T00:00:00Z",
        "status": "filtered",
        "reason": "compression_ratio",
        "request_sent": True,
        "text_len": 42,
    }

    selected = select_candidates([stt], audio_root=audio_root, limit=10)
    results = run_scout(
        selected,
        generate=lambda _audio: "<|ko|>secondary candidate",
    )
    report = build_report(selected, results)

    assert selected[0]["trigger_reasons"] == ["compression_ratio"]
    assert selected[0]["groq_text"] == ""
    assert selected[0]["groq_text_alignment"] == "translation_unavailable"
    assert results[0]["sensevoice_text"] == "secondary candidate"
    assert results[0]["similarity"] is None
    assert results[0]["disagreement"] is None
    assert report["comparison_count"] == 0


def test_length_mismatch_blocks_sentence_text_from_engine_comparison(tmp_path):
    audio_root = tmp_path / "audio_dump"
    path = audio_root / "run-a" / "utt-3.wav"
    path.parent.mkdir(parents=True)
    sf.write(path, np.zeros(1600, dtype=np.float32), 16000)
    stt = {
        "event_type": "stt",
        "run_id": "run-a",
        "utterance_id": "utt-3",
        "created_at": "2026-07-11T00:00:00Z",
        "status": "success",
        "request_sent": True,
        "text_len": 99,
        "avg_logprob": -0.8,
    }
    translation = {
        "event_type": "translation",
        "run_id": "run-a",
        "status": "success",
        "source_count": 1,
        "source_utterance_ids": ["utt-3"],
        "evidence_source_utterance_ids": [],
        "source_text": "splitter prefix only",
    }

    selected = select_candidates(
        [stt, translation],
        audio_root=audio_root,
        limit=10,
    )

    assert selected[0]["groq_text"] == ""
    assert selected[0]["groq_text_alignment"] == "text_length_mismatch"


def test_missing_evidence_attribution_never_becomes_comparison_text(tmp_path):
    audio_root = tmp_path / "audio_dump"
    path = audio_root / "run-a" / "utt-4.wav"
    path.parent.mkdir(parents=True)
    sf.write(path, np.zeros(1600, dtype=np.float32), 16000)
    stt = {
        "event_type": "stt",
        "run_id": "run-a",
        "utterance_id": "utt-4",
        "status": "success",
        "request_sent": True,
        "text_len": 5,
        "avg_logprob": -0.8,
    }
    legacy_translation = {
        "event_type": "translation",
        "run_id": "run-a",
        "status": "success",
        "source_count": 1,
        "source_utterance_ids": ["utt-4"],
        "source_text": "12345",
    }

    selected = select_candidates(
        [stt, legacy_translation],
        audio_root=audio_root,
        limit=10,
    )

    assert selected[0]["groq_text"] == ""
    assert selected[0]["groq_text_alignment"] == "evidence_attribution_unavailable"


def test_forced_sentence_selects_audio_without_successful_translation(tmp_path):
    audio_root = tmp_path / "audio_dump"
    path = audio_root / "run-a" / "utt-5.wav"
    path.parent.mkdir(parents=True)
    sf.write(path, np.zeros(1600, dtype=np.float32), 16000)
    stt = {
        "event_type": "stt",
        "run_id": "run-a",
        "utterance_id": "utt-5",
        "status": "success",
        "request_sent": True,
        "text_len": 20,
        "avg_logprob": -0.2,
    }
    sentence = {
        "event_type": "sentence",
        "run_id": "run-a",
        "utterance_id": "utt-5",
        "source_utterance_ids": ["utt-5"],
        "forced": True,
        "cut_reason": "forced_blob",
    }

    selected = select_candidates([stt, sentence], audio_root=audio_root, limit=10)

    assert selected[0]["trigger_reasons"] == ["forced_cut"]
    assert selected[0]["sentence_forced_cut_reasons"] == ["forced_blob"]
    assert selected[0]["groq_text_alignment"] == "translation_unavailable"


def test_evidence_bearing_multi_source_translation_is_candidate_but_not_comparison(tmp_path):
    audio_root = tmp_path / "audio_dump"
    path = audio_root / "run-a" / "utt-6.wav"
    path.parent.mkdir(parents=True)
    sf.write(path, np.zeros(1600, dtype=np.float32), 16000)
    stt = {
        "event_type": "stt",
        "run_id": "run-a",
        "utterance_id": "utt-6",
        "status": "success",
        "request_sent": True,
        "text_len": 20,
        "avg_logprob": -0.8,
    }
    translation = {
        "event_type": "translation",
        "run_id": "run-a",
        "status": "success",
        "source_count": 2,
        "source_utterance_ids": ["utt-0", "utt-6"],
        "evidence_source_utterance_ids": ["utt-0"],
        "source_text": "not one wav",
    }

    selected = select_candidates([stt, translation], audio_root=audio_root, limit=10)

    assert selected[0]["trigger_reasons"] == ["low_logprob"]
    assert selected[0]["groq_text"] == ""
    assert selected[0]["groq_text_alignment"] == "evidence_bearing_translation"
