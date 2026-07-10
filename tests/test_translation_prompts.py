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


_TRANSLATION_PROFILE_DATA_HASH = "03d1662dde319bc584f159828b48733c16b031010d0c28c044c3461d4fa0ce84"


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

    for qwen in (False, True):
        profile = get_translation_profile("hades_chxxnnx", qwen=qwen)
        for term in required_terms:
            assert term in profile


def test_hades_translation_profile_alias_uses_canonical_profile():
    for qwen in (False, True):
        assert get_translation_profile("hades", qwen=qwen) == \
            get_translation_profile("hades_chxxnnx", qwen=qwen)


def test_hades_translation_profile_examples_follow_canonical_names():
    forbidden_in_outputs = ("챈나", "솜펀치", "연초록", "큐마", "싱귤")

    for qwen in (False, True):
        profile = get_translation_profile("hades_chxxnnx", qwen=qwen)
        output_lines = [
            line for line in profile.splitlines()
            if line.startswith("output:")
        ]
        for line in output_lines:
            for forbidden in forbidden_in_outputs:
                assert forbidden not in line


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
        assert "해둥이" in profile
        assert "Yuni" in profile
        assert "楓之谷" in profile
        assert "투니버스 메들리" in profile


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
        "조금 더 가까이",
        "사계",
    )

    for qwen in (False, True):
        profile = get_translation_profile("url", qwen=qwen)
        for term in required_terms:
            assert term in profile


def test_standard_and_qwen_profile_glossary_facts_stay_in_sync():
    """standard/qwen are two hand-written renderings of the same facts.

    Nothing else guarantees they teach the same proper nouns, so compare the
    Hangul terms on glossary/mapping lines (before the first example) and
    require exact agreement. Example sentences may differ freely."""
    import re

    standard_profiles, qwen_profiles = _load_translation_profiles(_PROFILE_DATA_PATH)
    example_marker = re.compile(r"^(例\s?\d|\d+\.\s)")
    hangul_term = re.compile(r"[가-힣]{2,}")

    def glossary_hangul_terms(text: str) -> set[str]:
        terms: set[str] = set()
        for line in text.splitlines():
            if example_marker.match(line.strip()):
                break
            low = line.lower()
            if "->" in line or "=" in line or "keep" in low or "renderings" in low or "names:" in low:
                terms.update(hangul_term.findall(line))
        return terms

    for profile_id, standard_text in standard_profiles.items():
        standard_terms = glossary_hangul_terms(standard_text)
        qwen_terms = glossary_hangul_terms(qwen_profiles[profile_id])
        assert standard_terms == qwen_terms, (
            f"{profile_id} glossary drift — only standard: "
            f"{sorted(standard_terms - qwen_terms)}, only qwen: {sorted(qwen_terms - standard_terms)}"
        )


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


# ---------------------------------------------------------------------------
# current_activity background line (manual session state)
# ---------------------------------------------------------------------------

from contextlib import contextmanager

from config import cfg
from modules.translator import _compose_system_prompt


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


def test_output_rule_tail_stays_after_profile_and_activity():
    from modules.translation_prompts import _BASE_PROMPT_TAIL, _QWEN_PROMPT_TAIL

    with _activity("StarCraft"):
        prompt = _compose_system_prompt()
    assert prompt.endswith(_BASE_PROMPT_TAIL) or prompt.endswith(_QWEN_PROMPT_TAIL)
    # The tail must come after every appended section, never mid-prompt.
    tail = _QWEN_PROMPT_TAIL if prompt.endswith(_QWEN_PROMPT_TAIL) else _BASE_PROMPT_TAIL
    assert prompt.index(tail) > prompt.index("[Background] Current stream activity")


def test_prompts_contain_no_simplified_chinese():
    # zh-TW output prompts must not model simplified characters themselves.
    from modules.translation_prompts import _BASE_PROMPT, _QWEN_PROMPT

    simplified_only = set(
        "专乱开关记欢过还发货质变电时东车书长门问间闻马气汉当体优点级农应对争众"
        "伞传倾儿卫认误说读语请谁调贝购贵费资赛选逊适释镇风飞馆惊养鱼鸟鸡黄齐"
        "识别档况隐显热处译词汇后检简乐动听爱现观视觉罗网络终结绝给统继续维缩"
    )
    for name, prompt in (("_BASE_PROMPT", _BASE_PROMPT), ("_QWEN_PROMPT", _QWEN_PROMPT)):
        leaked = sorted({c for c in prompt if c in simplified_only})
        assert not leaked, f"{name} contains simplified chars: {leaked}"
