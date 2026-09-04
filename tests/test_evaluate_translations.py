import json
from pathlib import Path

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


def test_t25_bounded_eval_cases_freeze_approved_contrasts():
    cases_path = Path(__file__).resolve().parents[1] / "data" / "semantic_quality_eval_20260802.json"
    raw_cases = json.loads(cases_path.read_text(encoding="utf-8"))
    cases = load_eval_cases(cases_path)

    assert [row["source_annotation_id"] for row in raw_cases] == [
        50,
        51,
        52,
        58,
        60,
        65,
        66,
        71,
        72,
        77,
        80,
        82,
        85,
        91,
        93,
        94,
    ]
    assert summarize(evaluate_cases(cases))["passed"] == 16

    current_outputs = {
        row["id"]: row["current_output"]
        for row in raw_cases
    }
    current_summary = summarize(evaluate_cases(cases, current_outputs))
    assert current_summary["total"] == 16
    assert current_summary["passed"] == 0
    assert current_summary["failed"] == 16


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


def test_expected_empty_and_minimum_term_counts_are_deterministic():
    empty_case = _case(
        reference_output="",
        expected_empty=True,
        recoverability="stt_unrecoverable",
        dimensions=("noise",),
    )
    repeat_case = _case(min_term_counts=(("謝謝", 2),))

    assert evaluate_case(empty_case, "").passed
    assert evaluate_case(empty_case, "合理化的故事").failures == ("expected_empty",)
    assert "term_count<2:謝謝:1" in evaluate_case(repeat_case, "謝謝").failures
    assert evaluate_case(repeat_case, "謝謝，謝謝").passed


def test_suite_fixture_covers_recoverability_dimensions_and_exclusions():
    path = (
        Path(__file__).resolve().parents[1]
        / "data"
        / "semantic_quality_eval_20260812.json"
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    cases = load_eval_cases(path)

    assert {item["run_id"] for item in raw["excluded_runs"]} == {
        "20260812T082422Z-24676",
        "20260812T084207Z-28184",
    }
    assert len(cases) >= 16
    assert {case.recoverability for case in cases} == {
        "translatable",
        "currently_mistranslated",
        "partially_recoverable",
        "stt_unrecoverable",
    }
    assert {dimension for case in cases for dimension in case.dimensions} >= {
        "faithfulness",
        "hallucination",
        "entity",
        "context",
        "role",
        "domain",
        "noise",
    }
    assert summarize(evaluate_cases(cases))["failed"] == 0

    current_outputs = {
        row["id"]: row["current_output"] for row in raw["cases"]
    }
    current_summary = summarize(evaluate_cases(cases, current_outputs))
    assert 0 < current_summary["passed"] < current_summary["total"]
    assert current_summary["groups"]["dimension:role"]["failed"] >= 2
    assert current_summary["groups"]["requirement:activity"]["total"] >= 4
    assert current_summary["groups"]["requirement:audio"]["total"] >= 3


def test_suite_rejects_case_from_excluded_run(tmp_path):
    path = tmp_path / "suite.json"
    path.write_text(
        json.dumps(
            {
                "excluded_runs": [{"run_id": "polluted", "reason": "playback"}],
                "cases": [
                    {
                        "id": "bad",
                        "runtime_ref": {"run_id": "polluted", "sequence_id": 1},
                        "source_text": "안녕",
                        "reference_output": "你好",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="references excluded run"):
        load_eval_cases(path)


def test_provenance_suite_requires_exact_or_declared_derived_runtime_ref(tmp_path):
    artifact = tmp_path / "runtime.jsonl"
    artifact.write_text(
        json.dumps(
            {
                "event_type": "translation",
                "run_id": "natural-run",
                "sequence_id": 1,
                "source_text": "원문 전체",
            },
            ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )
    base = {
        "schema_version": 1,
        "dataset_id": "natural",
        "provenance_policy": "natural_runtime_and_derived_policy",
        "source_artifacts": ["runtime.jsonl"],
        "excluded_runs": [],
    }
    path = tmp_path / "suite.json"
    path.write_text(
        json.dumps(
            {
                **base,
                "cases": [
                    {
                        "id": "untraceable",
                        "source_text": "안녕",
                        "reference_output": "你好",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="exactly one"):
        load_eval_cases(path)

    path.write_text(
        json.dumps(
            {
                **base,
                "cases": [
                    {
                        "id": "derived",
                        "derived_from_runtime_ref": {
                            "run_id": "natural-run",
                            "sequence_id": 1,
                            "transformation": "removed recoverable suffix",
                        },
                        "source_text": "깨진 조각",
                        "reference_output": "",
                        "expected_empty": True,
                        "recoverability": "stt_unrecoverable",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert load_eval_cases(path)[0].case_id == "derived"

    path.write_text(
        json.dumps(
            {
                **base,
                "cases": [
                    {
                        "id": "wrong-source",
                        "runtime_ref": {
                            "run_id": "natural-run",
                            "sequence_id": 1,
                        },
                        "source_text": "다른 원문",
                        "reference_output": "不同原文",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="does not match runtime_ref"):
        load_eval_cases(path)


def test_evaluate_cases_treats_an_empty_result_set_as_missing_outputs():
    result = evaluate_cases([_case()], {})[0]

    assert not result.passed
    assert result.failures == ("missing_output",)


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


def test_main_can_evaluate_embedded_current_baseline():
    path = (
        Path(__file__).resolve().parents[1]
        / "data"
        / "semantic_quality_eval_20260812.json"
    )
    assert main(["--cases", str(path), "--use-current-output"]) == 1
