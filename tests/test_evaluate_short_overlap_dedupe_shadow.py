from scripts.evaluate_short_overlap_dedupe_shadow import find_short_overlap_candidates


def test_finds_four_character_overlap_only_when_audio_overlaps():
    translations = [
        {
            "run_id": "run-1",
            "sequence_id": 1,
            "source_text": "엥? 안해... 메이플?",
            "source_utterance_ids": ["utt-1"],
        },
        {
            "run_id": "run-1",
            "sequence_id": 2,
            "source_text": "메이플? 그거 살짝 그거잖아.",
            "source_utterance_ids": ["utt-2"],
        },
    ]
    stt_events = {("run-1", "utt-2"): {"overlap_seconds": 0.4}}

    candidates = find_short_overlap_candidates(stt_events, translations)

    assert len(candidates) == 1
    assert candidates[0]["removed_prefix"] == "메이플?"
    assert candidates[0]["shadow_source_text"] == "그거 살짝 그거잖아."
    assert candidates[0]["previous_last_source_utterance_id"] == "utt-1"
    assert candidates[0]["first_source_utterance_id"] == "utt-2"


def test_ignores_same_text_without_audio_overlap():
    translations = [
        {"run_id": "run-1", "sequence_id": 1, "source_text": "앞 문장 반복?", "source_utterance_ids": ["utt-1"]},
        {"run_id": "run-1", "sequence_id": 2, "source_text": "반복? 실제 반복", "source_utterance_ids": ["utt-2"]},
    ]
    stt_events = {("run-1", "utt-2"): {"overlap_seconds": 0.0}}

    assert find_short_overlap_candidates(stt_events, translations) == []
