import json

import pytest

from scripts.evaluate_translations import (
    EvalCase,
    DEFAULT_CASES_PATH,
    evaluate_case,
    evaluate_cases,
    load_eval_cases,
    load_outputs,
    main,
    summarize,
)


def _case(**overrides) -> EvalCase:
    values = {
        "case_id": "case",
        "source_text": "안녕하세요",
        "reference_output": "大家好",
        "incomplete": False,
        "categories": ("normal",),
        "note": "",
        "expected_terms": (),
        "forbidden_terms": (),
        "max_korean_ratio": 0.0,
        "max_japanese_chars": 0,
        "max_output_ratio": 3.0,
    }
    values.update(overrides)
    return EvalCase(**values)


def test_default_eval_cases_are_valid_and_reference_outputs_pass():
    cases = load_eval_cases(DEFAULT_CASES_PATH)
    results = evaluate_cases(cases)
    summary = summarize(results)

    assert len(cases) >= 6
    assert summary["failed"] == 0


def test_evaluate_case_flags_empty_and_identical_output():
    case = _case(source_text="안녕하세요", max_korean_ratio=1.0)

    empty = evaluate_case(case, "")
    identical = evaluate_case(case, "안녕하세요")

    assert "empty_output" in empty.failures
    assert "identical_to_source" in identical.failures


def test_evaluate_case_checks_korean_japanese_ratio_and_length():
    case = _case(
        source_text="안녕하세요",
        max_korean_ratio=0.0,
        max_japanese_chars=0,
        max_output_ratio=1.0,
    )

    result = evaluate_case(case, "안녕 こんにちは 這是一段很長很長的輸出")

    assert any(failure.startswith("korean_ratio>") for failure in result.failures)
    assert any(failure.startswith("japanese_chars>") for failure in result.failures)
    assert any(failure.startswith("output_ratio>") for failure in result.failures)


def test_evaluate_case_checks_expected_and_forbidden_terms():
    case = _case(
        expected_terms=("Minecraft",),
        forbidden_terms=("마인크래프트",),
    )

    result = evaluate_case(case, "今天玩 마인크래프트")

    assert "missing_expected:Minecraft" in result.failures
    assert "forbidden_term:마인크래프트" in result.failures


def test_load_outputs_supports_dict_and_list_formats(tmp_path):
    dict_file = tmp_path / "dict.json"
    list_file = tmp_path / "list.json"
    dict_file.write_text(json.dumps({"a": "output", "b": {"translation": "text"}}), encoding="utf-8")
    list_file.write_text(json.dumps([{"id": "a", "output": "output"}]), encoding="utf-8")

    assert load_outputs(dict_file) == {"a": "output", "b": "text"}
    assert load_outputs(list_file) == {"a": "output"}


def test_load_eval_cases_rejects_duplicate_ids(tmp_path):
    cases_file = tmp_path / "eval_cases.json"
    cases_file.write_text(
        json.dumps(
            [
                {"id": "a", "source_text": "안녕", "reference_output": "你好"},
                {"id": "a", "source_text": "안녕", "reference_output": "你好"},
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate eval case id"):
        load_eval_cases(cases_file)


def test_main_returns_nonzero_when_result_fails(tmp_path):
    results_file = tmp_path / "results.json"
    results_file.write_text(json.dumps({"normal_greeting": ""}), encoding="utf-8")

    assert main(["--results", str(results_file)]) == 1


def test_main_returns_zero_for_reference_outputs():
    assert main([]) == 0
