from modules.semantic_terminology import resolve_semantic_terminology


def test_wuthering_waves_title_is_provider_independent():
    escrow = resolve_semantic_terminology("막 명조 막 이런 거면 그럴 수 있지.")

    assert escrow.active
    assert [term.rule_id for term in escrow.terms] == [
        "wuthering_waves_game_title"
    ]
    assert escrow.provider_source == "막 __LT_SEM_1__ 막 이런 거면 그럴 수 있지."
    assert (
        escrow.restore_provider_candidate("像__LT_SEM_1__那種")
        == "像鳴潮那種"
    )


def test_wuthering_waves_rule_does_not_match_longer_hangul_token():
    assert not resolve_semantic_terminology("명조코 분들이 많아요.").active


def test_confirmed_terms_are_exactly_escrowed_and_restored():
    cases = (
        ("내가 좀 사패가 되는 것 같아", "反社會人格"),
        ("닉, 닉값 하시면 안 되나요?", "名副其實"),
        ("준회가 짬 때린 것 같으면 연락해", "把事情丟給別人"),
    )
    for source, expected in cases:
        escrow = resolve_semantic_terminology(source)
        assert escrow.active
        assert escrow.provider_source.count("__LT_SEM_1__") == 1
        passed, reason = escrow.evaluate_provider_candidate("前文__LT_SEM_1__後文")
        assert passed, reason
        restored = escrow.restore_provider_candidate("前文__LT_SEM_1__後文")
        assert expected in restored
        assert escrow.evaluate_final(restored) == (True, "")


def test_nearby_ordinary_korean_does_not_activate():
    for source in ("사패거리", "닉네임 값이 뭐야?", "짬뽕 때리다", "잠 때리다"):
        assert not resolve_semantic_terminology(source).active


def test_placeholder_loss_duplication_and_mutation_fail_closed():
    escrow = resolve_semantic_terminology("닉값 하시면 안 되나요?")
    for candidate in (
        "名副其實嗎？",
        "__LT_SEM_1____LT_SEM_1__",
        "__LT_SEM_ONE__",
        "__LT_SEM_1____LT_SEM_9__",
    ):
        passed, reason = escrow.evaluate_provider_candidate(candidate)
        assert not passed
        assert reason == "semantic_terminology_placeholder_cardinality"


def test_multiple_occurrences_are_not_escrowed_in_v1():
    assert not resolve_semantic_terminology("닉값도 닉값이고 참 그렇다").active


def test_amplification_release_is_a_source_grounded_direction_anchor():
    escrow = resolve_semantic_terminology(
        "타현님 우클릭 누르셔서 증폭 풀어주시면 풀어주시면 됩니다."
    )
    assert [term.rule_id for term in escrow.terms] == ["amplification_release"]
    assert escrow.provider_source == (
        "타현님 우클릭 누르셔서 __LT_SEM_1__ 됩니다."
    )
    assert escrow.evaluate_provider_candidate("右鍵後__LT_SEM_1__即可") == (True, "")
    assert escrow.restore_provider_candidate("右鍵後__LT_SEM_1__即可") == (
        "右鍵後解除增幅即可"
    )
    for source in ("증폭 풀어주시면 됩니다", "증폭을 풀어주시면 됩니다"):
        resolved = resolve_semantic_terminology(source)
        assert [term.rule_id for term in resolved.terms] == [
            "amplification_release"
        ]


def test_amplification_release_does_not_generalize_from_unrelated_pulda():
    for source in (
        "긴장을 풀어주시면 됩니다",
        "문제를 풀어주시면 됩니다",
        "볼륨을 풀로 올려주세요",
        "마이크 증폭이 걸립니다",
        "마이크 해제해 주시면 됩니다",
    ):
        assert not resolve_semantic_terminology(source).active
