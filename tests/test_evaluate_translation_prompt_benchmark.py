import json
import sys
from pathlib import Path

import pytest

from scripts import evaluate_translation_prompt_benchmark as tool


def _suite():
    return tool.load_suite()


def _case(case_id: str):
    return next(case for case in _suite()["cases"] if case["case_id"] == case_id)


def test_current_suite_has_exactly_75_unique_cases():
    suite = _suite()
    assert suite["case_count"] == 75
    assert len({case["case_id"] for case in suite["cases"]}) == 75
    contract = suite["production_message_contract"]
    assert contract["provider_message_builders"] == {
        "deepseek": "modules.translation_engines.build_effective_deepseek_messages",
        "openrouter": "modules.translation_engines.build_effective_qwen_messages",
    }
    assert contract["shared_history_and_current_input_structure"] is True
    assert "runtime_events_20260826.jsonl" in suite["runtime_sources"]
    assert "translations_20260826.txt" in suite["runtime_sources"]


def test_live26_asr_cases_freeze_positive_and_negative_controls():
    ambiguous = _case("tpb_live26_ambiguous_banban_074")
    assert ambiguous["case_role"] == "regression_control"
    assert ambiguous["origin"]["runtime_event_line"] == 60
    assert ambiguous["origin"]["translation_log"]["line"] == 15
    assert [row["check_id"] for row in ambiguous["semantic_expectations"]] == [
        "ambiguous_asr_no_guess"
    ]

    draw = _case("tpb_live26_draw_asr_075")
    assert draw["case_role"] == "expected_improvement"
    assert draw["origin"]["runtime_event_line"] == 64
    assert draw["origin"]["translation_log"]["line"] == 17
    checks = {row["check_id"]: row for row in draw["semantic_expectations"]}
    assert checks["draw_or_tie_meaning"]["kind"] == "required_any_substring"
    assert checks["manual_entry_meaning"]["kind"] == "required_any_substring"
    assert checks["no_mechanical_musongbu"]["kind"] == "forbidden_substring"


def test_load_suite_rejects_undeclared_provenance_source(tmp_path):
    suite = _suite()
    suite["runtime_sources"].remove("translations_20260826.txt")
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(suite, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="undeclared runtime source"):
        tool.load_suite(path)


def test_production_runtime_baseline_scores_all_cases_and_trace_stages():
    report = tool.evaluate_suite(_suite())
    assert report["mode"] == "production_runtime_baseline"
    assert report["case_count"] == 75
    assert len(report["cases"]) == 75
    assert report["aggregate"]["production_trace_cases"] == 15
    traced = next(row for row in report["cases"] if "final" in row["stages"])
    assert "first_provider_raw" in traced["stages"]
    assert "first_provider_corrected" in traced["stages"]
    assert "fallback_attempts" in traced["stages"]


def test_semantic_expectations_distinguish_current_sapa_failure():
    case = _case("tpb_live23_sapa_semantics_061")
    score = tool.score_candidate(
        case,
        case["production_trace"]["first_provider"]["raw_output"],
    )
    checks = {row["check_id"]: row["passed"] for row in score["semantic_checks"]}
    assert checks == {"sapa_sense": False, "no_sapa_insult": False}
    assert not score["semantic_literal_passed"]


def test_case_073_records_multi_asr_evidence_without_profanity_forbidden_gate():
    case = _case("tpb_live23_corrupted_english_073")
    assert case["failure_control_class"] == "mixed_language_source_fidelity"
    assert case["domain_evidence_scope"] == "current_source"
    assert case["source_evidence_status"] == "strong_multi_asr_support_human_pending"
    checks = {item["check_id"]: item["kind"] for item in case["semantic_expectations"]}
    assert checks == {
        "source_grounded_profanity": "manual",
        "mixed_language_source_fidelity": "manual",
    }
    assert all(item["kind"] != "forbidden_substring" for item in case["semantic_expectations"])


def test_canonical_and_script_gates_are_scored_separately():
    case = _case("tpb_irise_title_kiiri_003")
    good = tool.score_candidate(case, case["production_observation"]["target_text"])
    bad = tool.score_candidate(case, "基里說這是キリ的歌？")
    assert good["hard_gates"]["canonical_required"]
    assert good["hard_gates"]["canonical_forbidden"]
    assert good["hard_gates"]["script_compliance"]
    assert not bad["hard_gates"]["canonical_required"]
    assert not bad["hard_gates"]["canonical_forbidden"]
    assert not bad["hard_gates"]["script_compliance"]


@pytest.mark.parametrize(
    ("case_id", "target"),
    [
        ("tpb_irise_number_words_015", "總共有四種，當中六種可用。"),
        ("tpb_irise_ten_seconds_016", "請等十秒。"),
        ("tpb_hades_distance_054", "距離是三百四十公尺。"),
    ],
)
def test_number_gate_accepts_equivalent_traditional_chinese_numerals(case_id, target):
    assert tool.score_candidate(_case(case_id), target)["hard_gates"][
        "number_unit_preservation"
    ]


def test_number_gate_rejects_missing_or_digit_substring_values():
    case = _case("tpb_irise_number_words_015")
    assert not tool.score_candidate(case, "總共有四十六種。")['hard_gates'][
        "number_unit_preservation"
    ]


def test_deterministic_owners_are_reported_separately():
    case = _case("tpb_live23_sapa_semantics_061")
    ownership = tool.deterministic_ownership(
        case, case["production_observation"]["target_text"]
    )
    assert ownership["semantic_terminology_active"]
    assert ownership["semantic_terminology_rule_ids"] == ["sapa_antisocial"]
    assert "target_correction_changed" in ownership


def test_external_results_require_exact_case_coverage(tmp_path: Path):
    suite = _suite()
    rows = [
        {
            "case_id": case["case_id"],
            "target_text": case["production_observation"]["target_text"],
            "status": case["production_observation"]["status"],
            "subtitle_emitted": case["production_observation"]["subtitle_emitted"],
        }
        for case in suite["cases"]
    ]
    path = tmp_path / "results.json"
    path.write_text(json.dumps({"results": rows}, ensure_ascii=False), encoding="utf-8")
    report = tool.evaluate_suite(suite, results=tool._result_index(path))
    assert report["mode"] == "external_results"
    assert report["case_count"] == 75

    rows.pop()
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="coverage mismatch"):
        tool.evaluate_suite(suite, results=tool._result_index(path))


def test_cli_requires_exactly_one_input_mode(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["evaluate_translation_prompt_benchmark.py"])
    with pytest.raises(SystemExit, match="2"):
        tool.main()
