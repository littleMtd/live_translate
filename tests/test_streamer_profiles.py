import json

import pytest

from modules.streamer_profiles import (
    _PROFILE_DATA_PATH,
    _load_profile_data,
    build_stt_glossary,
    get_profile,
    known_profile_ids,
    profile_stt_terms,
)


def test_profile_data_file_is_valid_json():
    common_terms, profiles, aliases = _load_profile_data(_PROFILE_DATA_PATH)

    assert common_terms
    assert "" in profiles
    assert aliases["hades"] == "hades_chxxnnx"
    assert known_profile_ids() == frozenset(profiles)
    assert "hades" not in known_profile_ids()
    assert "hades" in known_profile_ids(include_aliases=True)


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


def test_profile_data_loader_rejects_duplicate_aliases(tmp_path):
    data_file = tmp_path / "streamer_profiles.json"
    data_file.write_text(
        json.dumps(
            {
                "common_stt_terms": [],
                "profiles": [
                    {"profile_id": "", "label": "General"},
                    {"profile_id": "a", "label": "A", "aliases": ["same"]},
                    {"profile_id": "b", "label": "B", "aliases": ["Same"]},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate streamer profile alias"):
        _load_profile_data(data_file)


def test_profile_data_loader_rejects_alias_that_conflicts_with_other_profile_id(tmp_path):
    data_file = tmp_path / "streamer_profiles.json"
    data_file.write_text(
        json.dumps(
            {
                "common_stt_terms": [],
                "profiles": [
                    {"profile_id": "", "label": "General"},
                    {"profile_id": "a", "label": "A", "aliases": ["b"]},
                    {"profile_id": "b", "label": "B"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="alias conflicts with profile id"):
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
    common_terms, profiles, _aliases = _load_profile_data(data_file)
    profile_terms = (*common_terms, *profiles["custom"].stt_terms)
    unique_terms = list(dict.fromkeys(term.strip() for term in profile_terms if term.strip()))

    assert unique_terms == ["shared", "common", "specific"]
    assert "Prefer exact spellings" in build_stt_glossary("stellive_hina")


def test_hades_stt_glossary_contains_profile_glossary_terms():
    glossary = build_stt_glossary("hades_chxxnnx")

    for term in (
        "챈나",
        "Chaenna",
        "CHXXNNX",
        "솜펀치",
        "띵띵이",
        "TINGGYUL",
        "키마",
        "큐마",
        "지옥견",
        "수제비",
        "마크",
        "섭주",
        "메이플",
        "프린세스 메이커",
        "피맛",
        "MEGA PIECE HARMONY",
        "Planet B",
        "하데쮸 유치원",
    ):
        assert term in glossary


def test_hades_alias_uses_hades_chxxnnx_terms():
    assert get_profile("hades").profile_id == "hades_chxxnnx"
    assert build_stt_glossary("hades") == build_stt_glossary("hades_chxxnnx")


def test_stellive_hina_stt_glossary_contains_current_profile_terms():
    glossary = build_stt_glossary("stellive_hina")

    for term in (
        "시라유키 히나",
        "Shirayuki Hina",
        "해둥이",
        "Haedungi",
        "아야츠노 유니",
        "Yuni",
        "메이플",
        "MapleStory",
        "투니버스 메들리",
        "포포 서버",
        "세키로",
        "Valorant",
    ):
        assert term in glossary


def test_stt_glossary_keeps_hades_terms_profile_bound():
    general_glossary = build_stt_glossary("")
    isegye_glossary = build_stt_glossary("isegye_lilpa")

    assert "챈나" not in general_glossary
    assert "챈나" not in isegye_glossary


def test_url_stt_glossary_contains_official_group_terms():
    glossary = build_stt_glossary("url")

    for term in (
        "UR:L",
        "유아렐",
        "유아엘",
        "YOU ARE LINKED",
        "결속아이돌",
        "모카",
        "랑코",
        "마냥",
        "솜먕",
        "오아",
        "바밍",
        "플럭서스",
        "㈜플럭서스",
        "Fluxus",
        "팬카페",
        "Chemical Love",
        "Again",
        "Wish Me Love",
        "조금 더 가까이",
        "사계",
    ):
        assert term in glossary


def test_irise_stt_glossary_contains_current_official_terms():
    glossary = build_stt_glossary("irise")

    for term in (
        "아이리제",
        "IRISÉ",
        "키리",
        "KIIRI",
        "티즈",
        "TIZ",
        "이제들",
        "이재들",
        "이재 여러분",
        "이재희 여러분",
        "IZÉ",
        "Parable Entertainment",
        "IRIDESCENT",
        "LOVEGAME",
        "Heart Crush",
    ):
        assert term in glossary

    # 이제 is an ordinary Korean adverb; do not bias STT globally toward the fandom.
    assert "이제" not in profile_stt_terms("irise")
