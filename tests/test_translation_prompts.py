import json
from dataclasses import replace

import pytest

from config import cfg
import modules.translation_prompts as translation_prompts
from modules.translation_engines import _compact_profile_digest, _deepseek_capsule_prompt
from modules.streamer_profiles import known_profile_ids
from modules.translation_prompts import (
    _PROFILE_DATA_PATH,
    _load_translation_profiles,
    get_translation_profile,
    get_translation_profile_facts,
    get_translation_profile_output_terms,
    get_translation_profile_preserve_terms,
    translation_profile_ids,
)
from scripts.update_translation_profile_snapshot import canonical_json_hash


_TRANSLATION_PROFILE_DATA_HASH = "5c807f9a8c3f90511b99235b398aea5a36053511e2f0c039c55d11c736ab5a35"


def test_translation_profile_data_snapshot_hash():
    assert canonical_json_hash(_PROFILE_DATA_PATH) == _TRANSLATION_PROFILE_DATA_HASH


def test_translation_profile_data_matches_streamer_registry():
    profiles = _load_translation_profiles(_PROFILE_DATA_PATH)
    expected_ids = known_profile_ids() - {""}

    assert frozenset(profiles) == expected_ids
    assert translation_profile_ids() == expected_ids


def test_unknown_translation_profile_returns_empty():
    assert get_translation_profile("unknown") == ""


def test_compact_profile_facts_exclude_examples():
    facts = get_translation_profile_facts("url")

    assert "유아렐" in facts
    assert "Wish Me Love" in facts
    assert "input:" not in facts


def test_profile_preserve_terms_are_derived_from_explicit_glossary_rules():
    url_terms = get_translation_profile_preserve_terms("url")

    assert {
        "모카",
        "랑코",
        "Wish Me Love",
        "조금 더 가까이",
        "Sandbox Network",
        "YOU ARE LINKED",
    } <= url_terms
    assert "Again" not in url_terms
    assert "유아렐" not in url_terms


def test_profile_preserve_terms_keep_only_canonical_self_mapping():
    stellive_terms = get_translation_profile_preserve_terms("stellive_hina")

    assert "해둥이" in stellive_terms
    assert "투니버스 메들리" in stellive_terms
    assert "해둥" not in stellive_terms
    assert "Haedungi" not in stellive_terms


def test_profile_output_terms_are_limited_to_explicit_glossary_canonicals():
    terms = get_translation_profile_output_terms("isegye_lilpa")

    assert {"Gosegu", "Jururu", "Lilpa", "Official髭男dism"} <= terms
    assert "고세구" not in terms
    assert "Everybody say" not in terms
    assert "이세계아이돌" in terms
    assert not any("only when" in term for term in get_translation_profile_output_terms("url"))


def test_profile_preserve_terms_include_later_official_title_rules():
    hades_terms = get_translation_profile_preserve_terms("hades_chxxnnx")

    assert "띵띵이" in hades_terms
    assert "MEGA PIECE HARMONY" in hades_terms
    assert "Planet B" in hades_terms
    assert "챈나" not in hades_terms


def test_hades_translation_profiles_contain_glossary_mappings():
    required_terms = (
        "Chaenna",
        "Chxxnnx",
        "CHXXNNX",
        "Sompunch",
        "Yeon Chorok",
        "Singgyul",
        "띵띵이",
        "Kyma",
        "지옥견",
        "수제비",
        "Kim Bongjun",
        "KimSungtae",
        "Minecraft",
        "服主",
        "服主房",
        "楓之谷",
        "美少女夢工場",
        "血味",
        "MEGA PIECE HARMONY",
        "Planet B",
    )

    profile = get_translation_profile("hades_chxxnnx")
    for term in required_terms:
        assert term in profile


def test_hades_translation_profile_alias_uses_canonical_profile():
    assert get_translation_profile("hades") == \
        get_translation_profile("hades_chxxnnx")


def test_hades_translation_profile_examples_follow_canonical_names():
    forbidden_in_outputs = ("챈나", "솜펀치", "연초록", "큐마", "싱귤")

    profile = get_translation_profile("hades_chxxnnx")
    output_lines = [
        line for line in profile.splitlines()
        if line.startswith("output:")
    ]
    for line in output_lines:
        for forbidden in forbidden_in_outputs:
            assert forbidden not in line


def test_isegye_translation_profiles_contain_official_romanization():
    profile = get_translation_profile("isegye_lilpa")
    assert "Gosegu" in profile
    assert "Jururu" in profile
    assert "Lilpa" in profile
    assert "히게단" in profile
    assert "Official髭男dism" in profile


def test_isegye_group_profile_preserve_terms_are_source_names():
    terms = get_translation_profile_preserve_terms("isegye_lilpa")
    assert {"이세계아이돌", "이파리", "둘기", "박쥐단", "주폭도", "세균단"} <= terms
    assert not {"똥강아지", "라니", "이세돌", "李世乭", "관"} & terms
    standard = get_translation_profile("isegye_lilpa")
    assert all(f"例 {n}" in standard for n in range(62, 74))


def test_isegye_production_prompts_include_the_profile():
    capsule = _deepseek_capsule_prompt("isegye_lilpa")
    for term in (
        "Official髭男dism",
        "Parable Entertainment",
        "李世乭",
        "세구땅",
        "Smile For You",
        "input:",  # DeepSeek gets the examples too
    ):
        assert term in capsule

    digest = _compact_profile_digest("isegye_lilpa")
    assert len(digest) < 1100
    assert "Official髭男dism" in digest
    assert "input:" not in digest  # Groq gets the glossary block only


def test_stellive_translation_profiles_contain_official_romanization():
    profile = get_translation_profile("stellive_hina")
    assert "Shirayuki Hina" in profile
    assert "해둥이" in profile
    assert "Yuni" in profile
    assert "楓之谷" in profile
    assert "투니버스 메들리" in profile
    for name in ("Shizuki Toa", "Kogane Lang", "Hanayo Hiyori", "Tokino Meri", "ABYSS"):
        assert name in profile


def test_url_translation_profiles_contain_official_group_terms():
    required_terms = (
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
        "Fluxus",
        "Chemical Love",
        "Again",
        "Wish Me Love",
        "Lullaby",
        "Lowkey",
        "로우키",
        "조금 더 가까이",
        "사계",
    )

    profile = get_translation_profile("url")
    for term in required_terms:
        assert term in profile


def test_irise_translation_profiles_keep_names_and_fandom_collision_safe():
    required_terms = (
        "IRISÉ",
        "아이리제",
        "KIIRI",
        "키리",
        "TIZ",
        "티즈",
        "IZÉ",
        "이제들",
        "이재들",
        "이재 여러분",
        "이재희 여러분",
        "Parable Entertainment",
        "IRIDESCENT",
        "LOVEGAME",
        "Heart Crush",
    )

    profile = get_translation_profile("irise")
    for term in required_terms:
        assert term in profile
    assert "이제 여러분들도 많이 기다려" in profile
    assert "output:現在各位應該也等了很久" in profile
    assert "never add" in profile or "must never leak" in profile

    output_terms = get_translation_profile_output_terms("irise")
    assert {"IRISÉ", "KIIRI", "TIZ", "IZÉ", "Parable Entertainment", "Wings"} <= output_terms
    assert not any("translate normally" in term for term in output_terms)
    assert not any("only in clear fandom" in term for term in output_terms)


def test_translation_profile_loader_rejects_non_string_values(tmp_path):
    data_file = tmp_path / "translation_profiles.json"
    data_file.write_text(
        json.dumps(
            {
                "profiles": {"profile_a": ["not text"]},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="map strings to strings"):
        _load_translation_profiles(data_file)


# ---------------------------------------------------------------------------
# current_activity background line (manual session state)
# ---------------------------------------------------------------------------

from contextlib import contextmanager

from config import cfg
from modules.translation_engines import effective_system_prompt_for_engine


def _compose_system_prompt() -> str:
    """The DeepSeek system prompt: where the activity capsule actually lands."""
    return effective_system_prompt_for_engine("deepseek", "")


@contextmanager
def _activity(value: str, *, use_profile: bool = True):
    original_activity = getattr(cfg.translation, "current_activity", "")
    original_use = cfg.translation.use_profile
    object.__setattr__(cfg.translation, "current_activity", value)
    object.__setattr__(cfg.translation, "use_profile", use_profile)
    try:
        yield
    finally:
        object.__setattr__(cfg.translation, "current_activity", original_activity)
        object.__setattr__(cfg.translation, "use_profile", original_use)


def test_current_activity_injected_as_labeled_background_line():
    with _activity("StarCraft"):
        prompt = _compose_system_prompt()
    assert "[Background] Current stream activity: StarCraft" in prompt
    assert "Never translate" in prompt


def test_current_activity_is_normalized_to_one_bounded_capsule():
    with _activity("  StarCraft   ladder  " + ("x" * 100)):
        prompt = _compose_system_prompt()

    background = next(
        line for line in prompt.splitlines()
        if line.startswith("[Background] Current stream activity:")
    )
    assert "\t" not in background
    assert "StarCraft ladder" in background
    assert len(background.removeprefix("[Background] Current stream activity: ")) <= 80
    assert prompt.count("[Background] Current stream activity:") == 1


def test_current_activity_applies_even_without_profile():
    with _activity("StarCraft", use_profile=False):
        prompt = _compose_system_prompt()
    assert "[Background] Current stream activity: StarCraft" in prompt


def test_empty_current_activity_leaves_prompt_untouched():
    with _activity("  "):
        with_blank = _compose_system_prompt()
    with _activity(""):
        without = _compose_system_prompt()
    assert with_blank == without
    assert "[Background]" not in without


def test_current_activity_changes_prompt_hence_cache_version():
    with _activity(""):
        base = _compose_system_prompt()
    with _activity("StarCraft"):
        with_activity = _compose_system_prompt()
    assert base != with_activity  # prompt_ver derives from the prompt → cache splits correctly


def test_prompts_contain_no_simplified_chinese():
    # zh-TW output prompts must not model simplified characters themselves.
    simplified_only = set(
        "专乱开关记欢过还发货质变电时东车书长门问间闻马气汉当体优点级农应对争众"
        "伞传倾儿卫认误说读语请谁调贝购贵费资赛选逊适释镇风飞馆惊养鱼鸟鸡黄齐"
        "识别档况隐显热处译词汇后检简乐动听爱现观视觉罗网络终结绝给统继续维缩"
    )
    for name in ("deepseek", "groq"):
        prompt = effective_system_prompt_for_engine(name, "")
        leaked = sorted({c for c in prompt if c in simplified_only})
        assert not leaked, f"{name} contains simplified chars: {leaked}"
