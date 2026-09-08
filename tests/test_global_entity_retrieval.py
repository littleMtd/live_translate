from modules.entity_registry import (
    ENTITY_REGISTRY,
    EntityAlias,
    EntityRegistry,
    EntityTranslationRule,
    ReviewedEntity,
)
from modules.translation_engines import (
    _deepl_context,
    _deepseek_system_prompt,
    _groq_system_prompt,
    _openrouter_system_prompt,
)
from modules.provisional_subtitles import provisional_fingerprint
from modules.profile_context import profile_state
from modules.translator import (
    Translator,
    _canonical_obligations_for_request,
    _resolve_entity_request_context,
)


def _ids(source: str) -> tuple[str, ...]:
    return tuple(
        activation.entity.entity_id
        for activation in _resolve_entity_request_context(source).activations
    )


def test_active_and_cross_profile_entities_use_same_global_registry_lookup():
    assert _ids("릴파가 왔어") == ("isegye_lilpa_member",)
    assert _ids("멤논이 왔어") == ("hades_memnon",)


def test_translation_lookup_excludes_stt_identity_and_ocr_only_aliases():
    # These are reviewed registry aliases, but have no translation_source scope.
    assert _ids("아이네와 HADES") == ()


def test_partial_latin_aliases_are_rejected():
    assert _ids("NotLilpa foo_Lilpa éLilpa LilpaExtra") == ()
    assert _ids("Lilpa가 왔어") == ("isegye_lilpa_member",)


def test_context_required_alias_needs_reviewed_name_context():
    assert _ids("마냥 행복해") == ()
    assert _ids("마냥님이 왔어") == ("url_manyang",)
    assert _ids("NotManyang님") == ()
    assert _ids("x마냥님 _마냥님 Manyang님Extra") == ()


def test_sompunch_ordinary_word_alias_requires_name_context():
    assert _ids("이번 주에 주먹이 나왔을 때 울었어") == ()
    assert _ids("주먹이님이 왔어") == ("hades_sompunch",)


def test_multiple_entities_create_only_the_mentioned_capsule_rows():
    context = _resolve_entity_request_context("릴파와 멤논이 만났어")
    assert tuple(a.entity.entity_id for a in context.activations) == (
        "isegye_lilpa_member",
        "hades_memnon",
    )
    assert "릴파 => Lilpa" in context.capsule
    assert "멤논 => Memnon" in context.capsule
    assert "Gosegu" not in context.capsule


def test_no_match_has_no_capsule_or_prompt_bloat():
    context = _resolve_entity_request_context("오늘은 날씨가 좋아")
    assert context.activations == ()
    assert context.capsule == ""


def test_cross_profile_required_entity_uses_existing_canonical_guard_contract():
    context = _resolve_entity_request_context("멤논이 왔어")
    obligations = _canonical_obligations_for_request("멤논이 왔어", context)
    assert tuple(item.canonical_target for item in obligations) == ("Memnon",)
    assert obligations[0].rule_id == "entity:hades_memnon"


def test_overlapping_aliases_coalesce_to_one_source_mention():
    context = _resolve_entity_request_context("릴파님이 왔어")
    activation = context.activations[0]
    assert activation.source_spans == ((0, 3),)
    assert len(context.obligations) == 1


def test_request_capsule_survives_all_compact_llm_prompts():
    source_prompt = (
        "base\n\n[Request entity mappings]\n- 멤논 => Memnon"
        "\n[End request entity mappings]"
    )
    for build in (_groq_system_prompt, _openrouter_system_prompt, _deepseek_system_prompt):
        prompt = build(source_prompt)
        assert prompt.count("[Request entity mappings]") == 1
        assert "멤논 => Memnon" in prompt

    deepl_context, _ = _deepl_context([], source_prompt)
    assert "멤논 => Memnon" in deepl_context


def test_lookup_is_read_only_with_respect_to_registry_identity():
    before = ENTITY_REGISTRY.identity
    profile_before = profile_state.current()
    _resolve_entity_request_context("릴파와 멤논이 만났어")
    assert ENTITY_REGISTRY.identity == before
    assert profile_state.current() == profile_before


def test_global_alias_collision_fails_closed():
    translation = EntityTranslationRule(
        "profile_a", (), "required", "always", "exact_alias", False
    )
    entities = tuple(
        ReviewedEntity(
            entity_id=f"entity_{index}",
            entity_type="person",
            canonical_source="Same",
            canonical_target=target,
            profile_ids=(profile,),
            aliases=(EntityAlias("Same", frozenset({"translation_source"})),),
            translation=translation if index == 1 else EntityTranslationRule(
                "profile_b", (), "required", "always", "exact_alias", False
            ),
        )
        for index, (target, profile) in enumerate(
            (("First", "profile_a"), ("Second", "profile_b")), start=1
        )
    )
    registry = EntityRegistry(1, "test", entities)
    assert registry.translation_mentions(
        "Same arrived", alias_matches=lambda *_args: True
    ) == ()


def test_capsule_changes_provisional_and_cache_shaping_prompt_identity():
    empty_prompt = _deepseek_system_prompt("base")
    capsule = _resolve_entity_request_context("멤논이 왔어").capsule
    entity_prompt = _deepseek_system_prompt("base\n\n" + capsule)
    assert empty_prompt != entity_prompt
    assert Translator._prompt_version(empty_prompt) != Translator._prompt_version(
        entity_prompt
    )

    common = dict(
        prepared_source="멤논이 왔어",
        source_utterance_ids=("u1",),
        evidence_source_utterance_ids=("u1",),
        profile_id="url",
        profile_cache_identity="profiles:1:url",
        activity_cache_identity="activity:none",
        history_cohort=("url", "activity:none", 1),
        incomplete=False,
    )
    without_entity = provisional_fingerprint(
        **common, messages=(("system", empty_prompt), ("user", "멤논이 왔어"))
    )
    with_entity = provisional_fingerprint(
        **common, messages=(("system", entity_prompt), ("user", "멤논이 왔어"))
    )
    assert without_entity != with_entity
