import json

import pytest

from modules.streamer_profiles import known_profile_ids
from modules.translation_prompts import (
    _PROFILE_DATA_PATH,
    _build_qwen_optimized_prompt,
    _load_translation_profiles,
    get_translation_profile,
    translation_profile_ids,
)
from scripts.update_translation_profile_snapshot import canonical_json_hash


_TRANSLATION_PROFILE_DATA_HASH = "73aba7801273ee613faf953fb409f946b97e84861e39d515625ebe141704cb3c"


def test_translation_profile_data_snapshot_hash():
    assert canonical_json_hash(_PROFILE_DATA_PATH) == _TRANSLATION_PROFILE_DATA_HASH


def test_translation_profile_data_matches_streamer_registry():
    standard_profiles, qwen_profiles = _load_translation_profiles(_PROFILE_DATA_PATH)
    expected_ids = known_profile_ids() - {""}

    assert frozenset(standard_profiles) == expected_ids
    assert frozenset(qwen_profiles) == expected_ids
    assert translation_profile_ids() == expected_ids
    assert translation_profile_ids(qwen=True) == expected_ids


def test_unknown_translation_profile_returns_empty():
    assert get_translation_profile("unknown") == ""
    assert get_translation_profile("unknown", qwen=True) == ""


def test_qwen_prompt_does_not_teach_placeholder_outputs():
    prompt = _build_qwen_optimized_prompt()

    assert "[UNK:" not in prompt
    assert "空字串" not in prompt
    assert "無法理解" not in prompt


def test_hades_translation_profiles_contain_glossary_mappings():
    required_terms = (
        "Chxxnnx",
        "Sompunch",
        "Yeon Chorok",
        "Singgyul",
        "Kyma",
        "Kim Bongjun",
        "KimSungtae",
        "Minecraft",
        "服主",
        "服主房",
        "楓之谷",
        "美少女夢工場",
        "血味",
    )

    for qwen in (False, True):
        profile = get_translation_profile("hades_chxxnnx", qwen=qwen)
        for term in required_terms:
            assert term in profile


def test_isegye_translation_profiles_contain_official_romanization():
    for qwen in (False, True):
        profile = get_translation_profile("isegye_lilpa", qwen=qwen)
        assert "Gosegu" in profile
        assert "Jururu" in profile
        assert "Lilpa" in profile


def test_stellive_translation_profiles_contain_official_romanization():
    for qwen in (False, True):
        profile = get_translation_profile("stellive_hina", qwen=qwen)
        assert "Shirayuki Hina" in profile


def test_translation_profile_loader_rejects_mismatched_ids(tmp_path):
    data_file = tmp_path / "translation_profiles.json"
    data_file.write_text(
        json.dumps(
            {
                "standard": {"profile_a": "text"},
                "qwen": {"profile_b": "text"},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="same ids"):
        _load_translation_profiles(data_file)


def test_translation_profile_loader_rejects_non_string_values(tmp_path):
    data_file = tmp_path / "translation_profiles.json"
    data_file.write_text(
        json.dumps(
            {
                "standard": {"profile_a": ["not text"]},
                "qwen": {"profile_a": "text"},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="map strings to strings"):
        _load_translation_profiles(data_file)
