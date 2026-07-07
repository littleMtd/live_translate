from contextlib import contextmanager

from config import cfg
from modules.translation_corrections import load_translation_corrections
from modules.translator import (
    _HADES_PROFILE_ID,
    _SHARED_NAME_SCOPE,
    _SOURCE_AWARE_TARGET_REPLACEMENTS,
    _SOURCE_NORM_BY_PROFILE,
    _PROFILE_SOURCE_AWARE_TARGET_REPLACEMENTS,
    _apply_source_aware_corrections,
    _normalize_source_before_matching,
    _NAME_RENDERING_RULES,
)


@contextmanager
def _active_translation_profile(profile_id: str, use_profile: bool = True):
    original_profile = cfg.translation.streamer_profile
    original_use_profile = cfg.translation.use_profile
    object.__setattr__(cfg.translation, "streamer_profile", profile_id)
    object.__setattr__(cfg.translation, "use_profile", use_profile)
    try:
        yield
    finally:
        object.__setattr__(cfg.translation, "streamer_profile", original_profile)
        object.__setattr__(cfg.translation, "use_profile", original_use_profile)


def _wrong_profile(profile_id: str) -> str:
    return "" if profile_id else _HADES_PROFILE_ID


def test_translation_correction_data_snapshot_counts():
    tables = load_translation_corrections()

    assert len(tables.source_aware_target_replacements) == 27
    assert {profile: len(values) for profile, values in tables.source_norm_by_profile.items()} == {
        "stellive_hina": 6,
        "hades_chxxnnx": 22,
        "mwmeu": 48,
    }
    assert {
        profile: len(groups)
        for profile, groups in tables.profile_source_aware_target_replacements.items()
    } == {"stellive_hina": 5}
    assert len(tables.korean_name_suffixes) == 32
    assert len(tables.name_rendering_rules) == 22
    assert sum(len(rule.wrong_forms) for rule in tables.name_rendering_rules) == 137
    assert sum(
        len(group.replacements)
        for group in tables.source_aware_target_replacements
    ) == 62
    assert sum(
        len(group.replacements)
        for groups in tables.profile_source_aware_target_replacements.values()
        for group in groups
    ) == 14


def test_each_profile_source_norm_rule_triggers_and_is_gated():
    for profile_id, replacements in _SOURCE_NORM_BY_PROFILE.items():
        for noisy, canonical in replacements.items():
            with _active_translation_profile(profile_id):
                assert _normalize_source_before_matching(noisy) == canonical

            with _active_translation_profile(profile_id, use_profile=False):
                assert _normalize_source_before_matching(noisy) == noisy

            with _active_translation_profile(_wrong_profile(profile_id)):
                assert _normalize_source_before_matching(noisy) == noisy


def test_each_global_source_aware_rule_triggers_and_is_source_gated():
    with _active_translation_profile(""):
        for source_terms, replacements, match_all in _SOURCE_AWARE_TARGET_REPLACEMENTS:
            source = " ".join(source_terms) if match_all else source_terms[0]
            for wrong, right in replacements:
                assert _apply_source_aware_corrections(source, wrong) == right
                assert _apply_source_aware_corrections("__unrelated_source__", wrong) == wrong
            if match_all and len(source_terms) > 1:
                # A partial match must not trigger an all-terms rule.
                partial = source_terms[0]
                for wrong, right in replacements:
                    assert _apply_source_aware_corrections(partial, wrong) == wrong


def test_each_profile_source_aware_rule_triggers_and_is_profile_gated():
    for profile_id, groups in _PROFILE_SOURCE_AWARE_TARGET_REPLACEMENTS.items():
        for source_terms, replacements, match_all in groups:
            source = " ".join(source_terms) if match_all else source_terms[0]
            for wrong, right in replacements:
                with _active_translation_profile(profile_id):
                    assert _apply_source_aware_corrections(source, wrong) == right

                with _active_translation_profile(profile_id, use_profile=False):
                    assert _apply_source_aware_corrections(source, wrong) == wrong

                with _active_translation_profile(_wrong_profile(profile_id)):
                    assert _apply_source_aware_corrections(source, wrong) == wrong


def test_each_name_rendering_rule_triggers_and_is_gated():
    for rule in _NAME_RENDERING_RULES:
        wrong = next(form for form in rule.wrong_forms if form != rule.canonical)
        source = rule.source_aliases[0]

        with _active_translation_profile(rule.scope if rule.scope != _SHARED_NAME_SCOPE else ""):
            assert _apply_source_aware_corrections(source, wrong) == rule.canonical
            assert _apply_source_aware_corrections("__unrelated_source__", wrong) == wrong

        if rule.scope != _SHARED_NAME_SCOPE:
            with _active_translation_profile(rule.scope, use_profile=False):
                assert _apply_source_aware_corrections(source, wrong) == wrong

            with _active_translation_profile(_wrong_profile(rule.scope)):
                assert _apply_source_aware_corrections(source, wrong) == wrong
