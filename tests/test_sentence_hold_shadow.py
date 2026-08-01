from modules.sentence_hold_shadow import (
    analyze_unfinished_tail,
    evaluate_next_chunk,
)


def test_detects_connector_particle_delimiter_and_forced_lexical_tail():
    connector = analyze_unfinished_tail("지금 게임 하고", forced=True)
    particle = analyze_unfinished_tail("오늘 나는", forced=True)
    adnominal = analyze_unfinished_tail(
        "예전에 자주 하던",
        forced=False,
        include_adnominal=True,
    )
    delimiter = analyze_unfinished_tail("오늘은 (진짜 좋아요", forced=False)
    lexical = analyze_unfinished_tail("이게 뭔가 이상한", forced=True)

    assert connector.signals == ("unfinished_connector",)
    assert connector.matched_ending == "하고"
    assert particle.signals == ("unfinished_particle",)
    assert particle.matched_ending == "는"
    assert adnominal.signals == ("unfinished_adnominal",)
    assert adnominal.matched_ending == "던"
    assert delimiter.signals == ("unclosed_delimiter",)
    assert delimiter.unclosed_delimiters == ("(",)
    assert lexical.signals == ("possible_truncated_lexical_tail",)


def test_short_grammatical_tail_requires_explicit_lower_floor():
    assert analyze_unfinished_tail("이거는", forced=False).signals == ()
    assert analyze_unfinished_tail(
        "이거는",
        forced=False,
        grammatical_min_significant=1,
    ).signals == ("unfinished_particle",)


def test_adnominal_signal_is_opt_in_for_t20_only():
    assert analyze_unfinished_tail(
        "예전에 자주 하던",
        forced=False,
    ).signals == ()


def test_complete_sentence_has_no_shadow_signal():
    assert analyze_unfinished_tail("오늘 정말 재미있어요", forced=False).signals == ()
    assert analyze_unfinished_tail("I can't say this all night", forced=False).signals == ()
    assert analyze_unfinished_tail("사과", forced=True).signals == ()
    assert analyze_unfinished_tail("최고", forced=True).signals == ()


def test_next_chunk_estimates_connector_merge_as_useful():
    candidate = analyze_unfinished_tail("지금 게임 하고", forced=True)

    result = evaluate_next_chunk("지금 게임 하고", "있어요", candidate)

    assert result["merged_text"] == "지금 게임 하고 있어요"
    assert result["merged_complete"] is True
    assert result["raw_continuation_heuristic"] is True
    assert result["useful_merge_heuristic"] is False


def test_next_chunk_can_close_delimiter_without_complete_ending():
    candidate = analyze_unfinished_tail("노래 제목은 「Again", forced=False)

    result = evaluate_next_chunk("노래 제목은 「Again", "」", candidate)

    assert result["delimiter_resolved"] is True
    assert result["useful_merge_heuristic"] is False  # delimiter alone is not meaningful speech


def test_meaningful_next_chunk_that_closes_delimiter_is_structurally_useful():
    candidate = analyze_unfinished_tail("노래 제목은 「Again", forced=False)

    result = evaluate_next_chunk(
        "노래 제목은 「Again",
        "정말 좋은 노래예요」",
        candidate,
    )

    assert result["delimiter_resolved"] is True
    assert result["structural_resolution"] is True
    assert result["useful_merge_heuristic"] is True


def test_unrelated_complete_sentence_does_not_resolve_weak_tail_signal():
    for text in ("오늘 먹은 사과", "오늘 기분 최고"):
        candidate = analyze_unfinished_tail(text, forced=True)
        result = evaluate_next_chunk(text, "오늘 방송 끝낼게요", candidate)
        assert result["useful_merge_heuristic"] is False
