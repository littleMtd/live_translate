import json

from scripts.evaluate_short_overlap_surgical import (
    _hangul_len,
    _removed_prefix,
    build_report,
)


def test_hangul_len_counts_only_hangul():
    assert _hangul_len("메이플?") == 3
    assert _hangul_len("재밌었다") == 4
    assert _hangul_len("a b c") == 0


def test_removed_prefix_at_lower_threshold():
    # "메이플?" (4 chars incl punct) survives default (>=5) but is removed at min_chars=4
    prev = "그래 메이플?"
    cur = "메이플? 그거 살짝 그거잖아 근데."
    assert _removed_prefix(prev, cur, 5) == ""  # default keeps it
    assert _removed_prefix(prev, cur, 4).startswith("메이플")


def test_build_report_sweeps_definitions_and_emits_audio_paths(tmp_path):
    path = tmp_path / "runtime_events_test.jsonl"
    events = [
        {"event_type": "stt", "run_id": "r1", "utterance_id": "utt-2", "overlap_seconds": 0.4},
        {"event_type": "translation", "status": "success", "run_id": "r1", "sequence_id": 1,
         "source_text": "그래 메이플?", "source_utterance_ids": ["utt-1"], "profile_id": "p"},
        {"event_type": "translation", "status": "success", "run_id": "r1", "sequence_id": 2,
         "source_text": "메이플? 그거 살짝 그거잖아 근데.", "source_utterance_ids": ["utt-2"],
         "profile_id": "p"},
    ]
    path.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")

    report = build_report([path], cps=7.0)
    assert report["candidate_union_count"] == 1
    c = report["candidates"][0]
    assert c["definitions"]["blunt_min4"] is True
    assert c["audio_path"] == "logs/audio_dump/r1/utt-2.wav"
    assert c["previous_audio_path"] == "logs/audio_dump/r1/utt-1.wav"
    assert c["current_audio_path"] == "logs/audio_dump/r1/utt-2.wav"
    assert c["removed_prefix_min4"].startswith("메이플")
    assert c["shadow_source_text_min4"].startswith("그거")
    # the short removed prefix fits any plausible overlap window -> cap is not the lever
    assert c["fits_overlap_window"] is True
    assert report["blast_radius_by_definition"]["surgical_min4_capped"] == \
        report["blast_radius_by_definition"]["blunt_min4"]
    assert report["review_candidate_definition"] == "blunt_min4"
    assert report["review_candidate_count"] == 1
    assert report["review_audio_complete_count"] == 0
