import json

import pytest

from modules.entity_registry import ENTITY_REGISTRY, load_entity_registry
from modules.profile_context import load_registry_snapshot
from modules.streamer_profiles import profile_stt_terms
from modules.translation_corrections import load_translation_corrections


def _write_registry(tmp_path, entities):
    path = tmp_path / "entities.json"
    path.write_text(
        json.dumps({"schema_version": 1, "entities": entities}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _entity(entity_id="one", alias="Name", target="Canonical", **extra):
    row = {
        "entity_id": entity_id,
        "entity_type": "person",
        "canonical_source": alias,
        "canonical_target": target,
        "profile_ids": ["url"],
        "aliases": [{"value": alias, "scopes": ["translation_source"]}],
    }
    row.update(extra)
    return row


def test_production_registry_loads_and_profiles_reference_real_entities():
    assert len(ENTITY_REGISTRY.entities) == 22
    snapshot = load_registry_snapshot(
        __import__("pathlib").Path("data/streamer_profiles.json"), version=7
    )
    assert snapshot.entity_registry is ENTITY_REGISTRY
    assert snapshot.entity_registry.entity("url_sommyang").canonical_target == "솜먕"


def test_alias_scopes_are_isolated_and_lookup_is_exact():
    roi = ENTITY_REGISTRY.exact_lookup(" 숨주먹 ", "identity_ocr", "hades_chxxnnx")
    assert roi and roi.entity_id == "hades_sompunch"
    assert ENTITY_REGISTRY.exact_lookup("숨주먹", "stt", "hades_chxxnnx") is None
    assert ENTITY_REGISTRY.exact_lookup("숨주먹", "translation_source", "hades_chxxnnx") is None
    assert ENTITY_REGISTRY.exact_lookup("prefix 숨주먹", "identity_ocr", "hades_chxxnnx") is None

    stt_only = ENTITY_REGISTRY.exact_lookup("이세돌", "stt", "isegye_lilpa")
    assert stt_only and stt_only.entity_id == "isegye_group"
    assert ENTITY_REGISTRY.exact_lookup("이세돌", "translation_source", "isegye_lilpa") is None

    translation_only = ENTITY_REGISTRY.exact_lookup("김랑코", "translation_source", "url")
    assert translation_only and translation_only.entity_id == "url_ranko"
    assert ENTITY_REGISTRY.exact_lookup("김랑코", "stt", "url") is None


def test_context_required_collision_policy_survives_migration():
    rules = {rule.canonical: rule for rule in load_translation_corrections().name_rendering_rules if rule.scope == "url"}
    assert rules["마냥"].activation_policy == "name_context_required"
    assert rules["오아"].activation_policy == "name_context_required"
    assert rules["오아"].repair_requires_name_context is True


def test_derived_stt_and_translation_views_keep_reviewed_behavior():
    assert profile_stt_terms("isegye_lilpa") == (
        "이세계아이돌", "이세돌", "릴파", "아이네", "징버거", "고세구", "주르르", "비챤"
    )
    rules = load_translation_corrections().name_rendering_rules
    assert len(rules) == 37
    lilpa = next(rule for rule in rules if rule.scope == "isegye_lilpa" and rule.canonical == "Lilpa")
    assert lilpa.publication_policy == "required"
    assert "늘파" in lilpa.source_aliases
    ranko = next(rule for rule in rules if rule.scope == "url" and rule.canonical == "랑코")
    assert ranko.source_aliases == ("랑코", "김랑코", "Ranko")


def test_loader_rejects_duplicate_ids_and_normalization_collisions(tmp_path):
    with pytest.raises(ValueError, match="duplicate entity_id"):
        load_entity_registry(_write_registry(tmp_path, [_entity(), _entity()]))

    two = _entity("two", "Ｎａｍｅ", "Other")
    with pytest.raises(ValueError, match="alias collision"):
        load_entity_registry(_write_registry(tmp_path, [_entity(), two]))


def test_loader_rejects_wrong_profile_and_scope_leakage(tmp_path):
    with pytest.raises(ValueError, match="unknown profiles"):
        load_entity_registry(
            _write_registry(tmp_path, [_entity()]), known_profiles=frozenset({""})
        )
    leaked = _entity(
        aliases=[
            {
                "value": "OCR typo",
                "scopes": ["identity_ocr"],
                "requires_context": True,
            }
        ]
    )
    with pytest.raises(ValueError, match="requires translation_source"):
        load_entity_registry(_write_registry(tmp_path, [leaked]))


def test_loader_rejects_missing_activation_and_preserves_alias_context_policy(tmp_path):
    missing = _entity(
        aliases=[{"value": "Only OCR", "scopes": ["identity_ocr"]}],
        translation={"scope": "url", "wrong_forms": []},
    )
    with pytest.raises(ValueError, match="lacks source activation"):
        load_entity_registry(_write_registry(tmp_path, [missing]))

    ambiguous = _entity(
        aliases=[
            {
                "value": "Name",
                "scopes": ["translation_source"],
                "requires_context": True,
            }
        ],
        translation={"scope": "url", "wrong_forms": []},
    )
    registry = load_entity_registry(_write_registry(tmp_path, [ambiguous]))
    assert registry.entities[0].aliases[0].requires_context is True
