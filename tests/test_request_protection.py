from modules.request_protection import (
    ProtectedSourceSpan,
    resolve_request_protection,
)


def test_one_request_contract_owns_resolved_unresolved_and_semantic_spans():
    source = "랑코가 하음이라는 멤버와 막 명조 막 이런 거면 그럴 수 있지"
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
    assert "하음" not in protection.provider_source
    assert "명조" not in protection.provider_source
    assert protection.provider_source.count("__LT_UNK_1__") == 1
    assert protection.provider_source.count("__LT_SEM_1__") == 1

    evaluation = protection.evaluate_provider_candidate(
        "랑코和__LT_UNK_1__一起玩__LT_SEM_1__。"
    )
    assert evaluation.passed
    assert evaluation.restored_candidate == "랑코和하음一起玩鳴潮。"
    assert protection.evaluate_final(evaluation.restored_candidate).passed


def test_request_contract_rejects_loss_and_has_mapping_sensitive_identity():
    source = "하음이라는 멤버가 왔어요"
    first = resolve_request_protection(source)
    second = resolve_request_protection(source)

    assert first.identity == second.identity
    assert not first.evaluate_provider_candidate("有位新成員來了。").passed
    assert not first.evaluate_final("有位新成員來了。").passed

    resolved = resolve_request_protection(
        source,
        resolved_spans=(ProtectedSourceSpan(
            owner="resolved_entity",
            source_text="하음",
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
