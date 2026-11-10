from modules.request_protection import (
    ProtectedSourceSpan,
    resolve_request_protection,
)


def test_provider_source_is_the_original_and_identity_tracks_mapping():
    source = "키야라는 멤버가 왔어요"
    first = resolve_request_protection(source)
    second = resolve_request_protection(source)

    assert first.identity == second.identity
    assert first.provider_source == source
    assert first.evaluate_provider_candidate("有位新成員來了。").passed

    resolved = resolve_request_protection(
        source,
        resolved_spans=(ProtectedSourceSpan(
            owner="resolved_entity",
            source_text="키야",
            source_spans=((0, 2),),
            rendering="Reviewed Target",
            rule_id="entity:reviewed",
        ),),
    )
    assert resolved.identity != first.identity
    assert resolved.provider_source == source


def test_invented_placeholders_are_rejected_without_any_issued():
    protection = resolve_request_protection("두빠님 별풍선 감사합니다.")

    for candidate, reason in (
        ("謝謝__LT_UNK_0__的星星氣球。", "unknown_name_invented_placeholder"),
        ("謝謝「__LT_UNK_n__」。", "unknown_name_invented_placeholder"),
        ("我變成__LT_SEM_1__了", "semantic_terminology_placeholder_cardinality"),
    ):
        provider = protection.evaluate_provider_candidate(candidate)
        assert not provider.passed
        assert provider.reason == reason
        final = protection.evaluate_final(candidate)
        assert not final.passed
        assert final.reason == reason


def test_ordinary_text_has_no_request_specific_cache_identity():
    protection = resolve_request_protection("오늘 방송 재미있어요")

    assert not protection.active
    assert protection.provider_source == protection.original_source
    assert protection.fingerprint_identity == ""


def test_resolved_entity_owns_source_proven_adjacent_honorific_residue():
    source = "랑코 님보다 낫다고 하면 안 되나요?"
    protection = resolve_request_protection(
        source,
        resolved_spans=(ProtectedSourceSpan(
            owner="resolved_entity",
            source_text="랑코",
            source_spans=((0, 2),),
            rendering="랑코",
            rule_id="entity:url_ranko",
        ),),
    )

    assert protection.spans[0].source_honorifics == ("님",)
    evaluation = protection.evaluate_provider_candidate(
        "不能說是比랑코님更好嗎？"
    )
    assert evaluation.passed
    assert evaluation.restored_candidate == "不能說是比랑코更好嗎？"


def test_entity_honorific_normalization_is_source_gated_and_count_bounded():
    source = "랑코보다 낫다고 하면 안 되나요?"
    span = ProtectedSourceSpan(
        owner="resolved_entity",
        source_text="랑코",
        source_spans=((0, 2),),
        rendering="랑코",
        rule_id="entity:url_ranko",
    )
    without_honorific = resolve_request_protection(
        source,
        resolved_spans=(span,),
    )
    assert without_honorific.evaluate_provider_candidate(
        "랑코님보다 낫다"
    ).restored_candidate == "랑코님보다 낫다"

    with_honorific = resolve_request_protection(
        "랑코 님과 랑코가 왔어요",
        resolved_spans=(ProtectedSourceSpan(
            owner="resolved_entity",
            source_text="랑코",
            source_spans=((0, 2), (5, 7)),
            rendering="랑코",
            rule_id="entity:url_ranko",
        ),),
    )
    assert with_honorific.evaluate_provider_candidate(
        "랑코님和랑코님來了"
    ).restored_candidate == "랑코和랑코님來了"
    assert with_honorific.evaluate_provider_candidate(
        "랑코님앗과 랑코님이來了"
    ).restored_candidate == "랑코님앗과 랑코來了"
    assert with_honorific.identity != without_honorific.identity

    lexical_continuation = resolve_request_protection(
        "랑코씨앗을 심었어요",
        resolved_spans=(span,),
    )
    assert lexical_continuation.spans[0].source_honorifics == ()

    particle_prefix_continuation = resolve_request_protection(
        "랑코님이상하다",
        resolved_spans=(span,),
    )
    assert particle_prefix_continuation.spans[0].source_honorifics == ()

    assert with_honorific.evaluate_provider_candidate(
        "랑코님이상하다와 랑코님이 來了"
    ).restored_candidate == "랑코님이상하다와 랑코 來了"


def test_non_hangul_canonical_is_not_rewritten_by_honorific_finalization():
    protection = resolve_request_protection(
        "릴파 님이 왔어요",
        resolved_spans=(ProtectedSourceSpan(
            owner="resolved_entity",
            source_text="릴파",
            source_spans=((0, 2),),
            rendering="Lilpa",
            rule_id="entity:isegye_lilpa",
        ),),
    )

    assert protection.spans[0].source_honorifics == ("님",)
    assert protection.evaluate_provider_candidate(
        "Lilpa님이來了"
    ).restored_candidate == "Lilpa님이來了"
