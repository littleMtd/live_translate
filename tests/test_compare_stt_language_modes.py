import json

from scripts.compare_stt_language_modes import (
    analyze_pair,
    build_report,
    iter_events,
    resume_pairs,
    run_pairs,
    select_candidates,
)


def _translation(run_id: str, utterance_id: str, text: str, **extra):
    return {
        "event_type": "translation",
        "status": "success",
        "run_id": run_id,
        "source_utterance_ids": [utterance_id],
        "source_count": 1,
        "source_text": text,
        "incomplete": False,
        **extra,
    }


def test_select_candidates_is_replayable_stratified_and_needs_no_annotations(tmp_path):
    audio_root = tmp_path / "audio_dump"
    events = [
        _translation("run", "utt-1", "안녕하세요"),
        _translation("run", "utt-2", "I am Iron Man", quality_flags=["low_source_hangul"]),
        _translation("run", "utt-3", "낮은 신뢰도", avg_logprob=-0.8),
        _translation("run", "utt-4", "今日は楽しい"),
    ]
    for event in events:
        path = audio_root / event["run_id"] / f"{event['source_utterance_ids'][0]}.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"wav")

    selected = select_candidates(events, audio_root=audio_root, limit=4)

    assert len(selected) == 4
    assert {tag for row in selected for tag in row["strata"]} >= {
        "baseline", "latin_heavy", "low_confidence", "kana_present"
    }
    assert all("annotation" not in row for row in selected)


def test_iter_events_ignores_invalid_json(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"event_type":"translation"}\ninvalid\n', encoding="utf-8")

    assert list(iter_events([path])) == [{"event_type": "translation"}]


def test_analyze_pair_flags_obvious_auto_detect_regression_proxies():
    comparison = analyze_pair(
        {"text": "오늘 정말 재미있었어요", "language": "ko"},
        {"text": "Today was fun", "language": "en"},
        "오늘 정말 재미있었어요",
    )

    assert comparison["changed"] is True
    assert comparison["comparable"] is True
    assert "auto_non_ko_on_hangul_baseline" in comparison["regression_proxy_flags"]
    assert "hangul_ratio_drop_ge_0_3" in comparison["regression_proxy_flags"]
    assert "introduced_latin_heavy" in comparison["regression_proxy_flags"]


def test_historical_kana_is_observation_signal_not_automatic_regression():
    comparison = analyze_pair(
        {"text": "구독과 좋아요", "language": "ko", "error": None},
        {"text": "ここ", "language": "Japanese", "error": None},
        "ココ!",
    )

    assert comparison["regression_proxy_flags"] == []
    assert comparison["observation_signals"] == ["auto_japanese_on_historical_kana"]


def test_run_pairs_and_report_never_claim_correctness(tmp_path):
    wav = tmp_path / "sample.wav"
    wav.write_bytes(b"wav")
    candidates = [{
        "run_id": "r1",
        "utterance_id": "utt-1",
        "audio_path": str(wav),
        "historical_source_text": "안녕하세요",
        "strata": ["baseline"],
    }]

    def transcribe(_path, mode):
        return {
            "text": "안녕하세요",
            "language": "ko",
            "latency_ms": 100 if mode == "fixed_ko" else 110,
            "error": None,
        }

    results = run_pairs(candidates, transcribe=transcribe, project_root=tmp_path)
    report = build_report(candidates, results)

    assert report["gate"] == "eligible_for_record_only_shadow"
    assert report["ground_truth_count"] == 0
    assert report["correctness_claim"] is None
    assert report["latency_ms"]["fixed_ko"]["mean"] == 100


def test_resume_pairs_retries_only_failed_side(tmp_path):
    wav = tmp_path / "sample.wav"
    wav.write_bytes(b"wav")
    candidate = {
        "run_id": "r1", "utterance_id": "utt-1", "audio_path": str(wav),
        "historical_source_text": "안녕하세요", "strata": ["baseline"],
    }
    previous = [{
        **candidate,
        "fixed_ko": {"text": "안녕하세요", "language": "ko", "error": None},
        "auto_detect": {"text": "", "language": "", "error": "RateLimitError"},
    }]
    calls = []

    def transcribe(_path, mode):
        calls.append(mode)
        return {"text": "안녕하세요", "language": "ko", "error": None}

    results = resume_pairs([candidate], previous, transcribe=transcribe, project_root=tmp_path)

    assert calls == ["auto_detect"]
    assert results[0]["comparison"]["comparable"] is True


def test_report_blocks_on_proxy_or_api_error():
    candidate = {"strata": ["baseline"]}
    proxy_result = {
        "fixed_ko": {"language": "ko", "latency_ms": 1, "error": None},
        "auto_detect": {"language": "en", "latency_ms": 1, "error": None},
        "comparison": {"regression_proxy_flags": ["introduced_latin_heavy"]},
    }
    assert build_report([candidate], [proxy_result])["gate"] == "no_go"

    error_result = {
        "fixed_ko": {"language": "", "latency_ms": 1, "error": "TimeoutError"},
        "auto_detect": {"language": "ko", "latency_ms": 1, "error": None},
        "comparison": {"regression_proxy_flags": []},
    }
    assert build_report([candidate], [error_result])["gate"] == "inconclusive_api_errors"


def test_api_error_pair_is_not_scored_as_model_regression():
    comparison = analyze_pair(
        {"text": "안녕하세요", "language": "ko", "error": None},
        {"text": "", "language": "", "error": "RateLimitError"},
    )

    assert comparison["comparable"] is False
    assert comparison["changed"] is None
    assert comparison["regression_proxy_flags"] == []
