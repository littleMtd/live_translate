from modules.source_fuzzy_shadow import (
    build_source_fuzzy_shadow,
    safe_source_fuzzy_shadow,
)


def test_unique_profile_term_match_is_record_only():
    source = "오늘 채나 방송 봤어"

    shadow = build_source_fuzzy_shadow(
        source,
        profile_id="hades_chxxnnx",
        use_profile=True,
        terms=("챈나", "팅규"),
        enabled=True,
    )

    assert shadow["mode"] == "record_only"
    assert shadow["applied"] is False
    assert shadow["candidate_count"] == 1
    assert shadow["unique_match_count"] == 1
    assert shadow["ambiguous_count"] == 0
    assert shadow["would_change"] is True
    assert shadow["proposed_text"] == "오늘 챈나 방송 봤어"
    assert shadow["candidates"][0]["observed"] == "채나"
    assert shadow["candidates"][0]["canonical"] == "챈나"


def test_close_runner_up_is_ambiguous_and_source_stays_unchanged():
    source = "가나"

    shadow = build_source_fuzzy_shadow(
        source,
        profile_id="hades_chxxnnx",
        use_profile=True,
        terms=("각나", "간나"),
        enabled=True,
    )

    assert shadow["candidate_count"] == 1
    assert shadow["unique_match_count"] == 0
    assert shadow["ambiguous_count"] == 1
    assert shadow["would_change"] is False
    assert shadow["proposed_text"] == source
    assert shadow["candidates"][0]["decision"] == "ambiguous"
    assert shadow["candidates"][0]["canonical"] == ""


def test_profile_disabled_and_non_profile_terms_never_match():
    source = "채나"

    disabled = build_source_fuzzy_shadow(
        source,
        profile_id="hades_chxxnnx",
        use_profile=False,
        terms=("챈나",),
        enabled=True,
    )
    unrelated = build_source_fuzzy_shadow(
        source,
        profile_id="hades_chxxnnx",
        use_profile=True,
        terms=("다른말", "English"),
        enabled=True,
    )

    assert disabled["reason"] == "profile_disabled"
    assert disabled["candidate_count"] == 0
    assert unrelated["candidate_count"] == 0
    assert unrelated["proposed_text"] == ""


def test_distant_or_length_mismatched_terms_are_ignored():
    shadow = build_source_fuzzy_shadow(
        "채나",
        profile_id="hades_chxxnnx",
        use_profile=True,
        terms=("챈나미", "완전히"),
        enabled=True,
    )

    assert shadow["candidate_count"] == 0
    assert shadow["would_change"] is False


def test_production_terms_exclude_unreviewed_targets_and_reviewed_aliases():
    shadow = build_source_fuzzy_shadow(
        "공부 포탈 해동이 팅귤 찬나 솜명 챈나룬 챈나로",
        profile_id="hades_chxxnnx",
        use_profile=True,
        enabled=True,
    )

    assert shadow["candidate_count"] == 0
    assert shadow["proposed_text"] == ""


def test_vocative_suffix_is_not_treated_as_name_miss():
    shadow = build_source_fuzzy_shadow(
        "해둥아 안녕",
        profile_id="stellive_hina",
        use_profile=True,
        enabled=True,
    )

    assert shadow["candidate_count"] == 0
    assert shadow["proposed_text"] == ""


def test_safe_wrapper_fails_closed(monkeypatch):
    monkeypatch.setattr(
        "modules.source_fuzzy_shadow._profile_terms",
        lambda _profile, _terms: (_ for _ in ()).throw(RuntimeError("bad profile")),
    )

    shadow = safe_source_fuzzy_shadow(
        "채나",
        profile_id="hades_chxxnnx",
        use_profile=True,
    )

    assert shadow["reason"] == "diagnostic_error"
    assert shadow["error_type"] == "RuntimeError"
    assert shadow["applied"] is False
    assert shadow["proposed_text"] == ""


def test_safe_wrapper_exception_path_cannot_rethrow_for_invalid_profile():
    shadow = safe_source_fuzzy_shadow(
        "찬나",
        profile_id=1,  # type: ignore[arg-type]
        use_profile=True,
    )

    assert shadow["reason"] == "diagnostic_error"
    assert shadow["profile_id"] == ""
    assert shadow["enabled"] is False
    assert shadow["applied"] is False
