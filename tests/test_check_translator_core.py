from scripts.check_translator_core import (
    check_eval_cases,
    check_json_fixtures,
    check_translation_profile_snapshot,
    main,
    run_checks,
)


def test_fast_translator_core_checks_pass():
    results = run_checks(include_pytest=False)

    assert all(result.passed for result in results)
    assert [result.name for result in results] == [
        "json_fixtures",
        "translation_profile_snapshot",
        "eval_cases",
    ]


def test_individual_fast_checks_pass():
    assert check_json_fixtures().passed
    assert check_translation_profile_snapshot().passed
    assert check_eval_cases().passed


def test_main_skip_pytest_returns_zero(capsys):
    result = main(["--skip-pytest"])

    captured = capsys.readouterr()
    assert result == 0
    assert "PASS json_fixtures" in captured.out
