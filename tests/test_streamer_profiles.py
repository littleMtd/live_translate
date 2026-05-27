import json

import pytest

from modules.streamer_profiles import (
    _PROFILE_DATA_PATH,
    _load_profile_data,
    build_stt_glossary,
    get_profile,
    known_profile_ids,
)


def test_profile_data_file_is_valid_json():
    common_terms, profiles = _load_profile_data(_PROFILE_DATA_PATH)

    assert common_terms
    assert "" in profiles
    assert known_profile_ids() == frozenset(profiles)


def test_unknown_profile_falls_back_to_general():
    profile = get_profile("missing-profile")

    assert profile.profile_id == ""
    assert profile.label == "General"


def test_profile_data_loader_rejects_duplicate_ids(tmp_path):
    data_file = tmp_path / "streamer_profiles.json"
    data_file.write_text(
        json.dumps(
            {
                "common_stt_terms": [],
                "profiles": [
                    {"profile_id": "", "label": "General"},
                    {"profile_id": "", "label": "Duplicate"},
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate streamer profile id"):
        _load_profile_data(data_file)


def test_profile_data_loader_requires_general_profile(tmp_path):
    data_file = tmp_path / "streamer_profiles.json"
    data_file.write_text(
        json.dumps(
            {
                "common_stt_terms": [],
                "profiles": [{"profile_id": "custom", "label": "Custom"}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="general profile"):
        _load_profile_data(data_file)


def test_build_stt_glossary_deduplicates_loaded_terms(tmp_path):
    data_file = tmp_path / "streamer_profiles.json"
    data_file.write_text(
        json.dumps(
            {
                "common_stt_terms": ["shared", " common ", ""],
                "profiles": [
                    {"profile_id": "", "label": "General"},
                    {
                        "profile_id": "custom",
                        "label": "Custom",
                        "stt_terms": ["shared", "specific"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    common_terms, profiles = _load_profile_data(data_file)
    profile_terms = (*common_terms, *profiles["custom"].stt_terms)
    unique_terms = list(dict.fromkeys(term.strip() for term in profile_terms if term.strip()))

    assert unique_terms == ["shared", "common", "specific"]
    assert "Prefer exact spellings" in build_stt_glossary("stellive_hina")


def test_hades_stt_glossary_contains_profile_glossary_terms():
    glossary = build_stt_glossary("hades_chxxnnx")

    for term in ("챈나", "키마", "마크", "섭주", "메이플", "프린세스 메이커", "피맛"):
        assert term in glossary


def test_stt_glossary_keeps_hades_terms_profile_bound():
    general_glossary = build_stt_glossary("")
    isegye_glossary = build_stt_glossary("isegye_lilpa")

    assert "챈나" not in general_glossary
    assert "챈나" not in isegye_glossary
