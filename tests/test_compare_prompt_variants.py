from scripts.compare_prompt_variants import (
    CASES,
    _legacy_profile_snapshot,
    build_report,
    evaluate_output,
)


def test_structural_report_compares_legacy_and_v2_without_api():
    report = build_report()

    assert report["executed"] is False
    assert report["prompt_chars"]["v2"] < report["prompt_chars"]["legacy"] * 0.7
    assert report["reduction_ratio"] > 0.3
    assert len(report["cases"]) == len(CASES)


def test_evaluator_accepts_required_terms_and_rejects_placeholder():
    case = {
        "contains": ("UR:L", "URL"),
        "require_cjk": True,
        "forbid": ("留空",),
    }

    assert evaluate_output(case, "把UR:L新歌的URL傳給我")["passed"] is True
    rejected = evaluate_output(case, "（留空）")
    assert rejected["passed"] is False
    assert "meta_or_placeholder_output" in rejected["failures"]


def test_url_legacy_profile_is_a_frozen_full_snapshot():
    profile = _legacy_profile_snapshot()

    assert "Four-member virtual idol group" in profile
    assert profile.count("input:") == 7
