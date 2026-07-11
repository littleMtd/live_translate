import numpy as np
import soundfile as sf

from scripts.scout_sensevoice_historical import build_report, run_scout, select_candidates


def test_select_candidates_requires_exact_single_utterance_audio(tmp_path):
    audio_root = tmp_path / "audio_dump"
    path = audio_root / "run-a" / "utt-1.wav"
    path.parent.mkdir(parents=True)
    sf.write(path, np.zeros(1600, dtype=np.float32), 16000)
    base = {
        "event_type": "translation",
        "run_id": "run-a",
        "created_at": "2026-07-11T00:00:00Z",
        "status": "success",
        "incomplete": False,
        "source_count": 1,
        "source_utterance_ids": ["utt-1"],
        "source_text": "안녕하세요",
        "quality_severity": "warn",
        "avg_logprob": -0.7,
    }

    selected = select_candidates(
        [base, {**base, "source_utterance_ids": ["utt-1", "utt-2"]}],
        audio_root=audio_root,
        limit=10,
    )

    assert len(selected) == 1
    assert selected[0]["utterance_id"] == "utt-1"


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
