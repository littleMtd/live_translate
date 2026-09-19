from modules.request_protection import (
    ProtectedSourceSpan,
    resolve_request_protection,
)


def test_one_request_contract_owns_resolved_unresolved_and_semantic_spans():
    source = "랑코가 키야라는 멤버와 막 명조 막 이런 거면 그럴 수 있지"
    protection = resolve_request_protection(
        source,
        resolved_spans=(ProtectedSourceSpan(
            owner="resolved_entity",
            source_text="랑코",
            source_spans=((0, 2),),
            rendering="랑코",
            rule_id="entity:rangko",
        ),),
        known_source_spans=((0, 2),),
    )

    assert [span.owner for span in protection.spans] == [
        "resolved_entity",
        "unresolved_referent",
        "semantic_term",
    ]
    assert "랑코" in protection.provider_source
    assert "키야" not in protection.provider_source
    assert "명조" not in protection.provider_source
    assert protection.provider_source.count("__LT_UNK_1__") == 1
    assert protection.provider_source.count("__LT_SEM_1__") == 1

    evaluation = protection.evaluate_provider_candidate(
        "랑코和__LT_UNK_1__一起玩__LT_SEM_1__。"
    )
    assert evaluation.passed
    assert evaluation.restored_candidate == "랑코和키야一起玩鳴潮。"
    assert protection.evaluate_final(evaluation.restored_candidate).passed


def test_request_contract_rejects_loss_and_has_mapping_sensitive_identity():
    source = "키야라는 멤버가 왔어요"
    first = resolve_request_protection(source)
    second = resolve_request_protection(source)

    assert first.identity == second.identity
    assert not first.evaluate_provider_candidate("有位新成員來了。").passed
    assert not first.evaluate_final("有位新成員來了。").passed

    resolved = resolve_request_protection(
        source,
        resolved_spans=(ProtectedSourceSpan(
            owner="resolved_entity",
            source_text="키야",
            source_spans=((0, 2),),
            rendering="Reviewed Target",
            rule_id="entity:reviewed",
        ),),
        known_source_spans=((0, 2),),
    )
    assert resolved.identity != first.identity
    assert not resolved.requires_provider_protection


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
        known_source_spans=((0, 2),),
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
        known_source_spans=((0, 2),),
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
        known_source_spans=((0, 2), (5, 7)),
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
        known_source_spans=((0, 2),),
    )
    assert lexical_continuation.spans[0].source_honorifics == ()

    particle_prefix_continuation = resolve_request_protection(
        "랑코님이상하다",
        resolved_spans=(span,),
        known_source_spans=((0, 2),),
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
        known_source_spans=((0, 2),),
    )

    assert protection.spans[0].source_honorifics == ("님",)
    assert protection.evaluate_provider_candidate(
        "Lilpa님이來了"
    ).restored_candidate == "Lilpa님이來了"
