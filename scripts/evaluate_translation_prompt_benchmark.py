"""Offline scorer for the current 75-case production translation benchmark.

This tool intentionally has no provider execution or prompt/model experiment
modes.  It scores the frozen production observation/trace, or a complete set
of externally-produced case results, using the benchmark's deterministic
semantic and publication expectations.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.activity_context import bind_profile_id
from modules.semantic_terminology import resolve_semantic_terminology
from modules.translator import (
    _apply_source_aware_corrections,
    _normalize_source_before_matching,
    _resolve_active_canonical_obligations,
)
from modules.unknown_name_escrow import resolve_unknown_name_escrow
from utils.runtime_events import translation_quality


DEFAULT_SUITE = ROOT / "data" / "translation_prompt_benchmark_20260822.json"
_KANA_RE = re.compile(r"[\u3040-\u30ff]")


def load_suite(path: Path = DEFAULT_SUITE) -> dict[str, Any]:
    suite = json.loads(path.read_text(encoding="utf-8"))
    cases = suite.get("cases")
    if not isinstance(cases, list) or len(cases) != suite.get("case_count"):
        raise ValueError("benchmark case_count does not match cases")
    case_ids = [case.get("case_id") for case in cases]
    if any(not isinstance(case_id, str) or not case_id for case_id in case_ids):
        raise ValueError("every benchmark case requires a case_id")
    if len(set(case_ids)) != len(case_ids):
        raise ValueError("benchmark case_id values must be unique")
    declared_sources = set(suite.get("runtime_sources") or [])
    for case in cases:
        origin = case.get("origin") or {}
        referenced_sources = [origin.get("runtime_event_file")]
        translation_log = origin.get("translation_log") or {}
        referenced_sources.append(translation_log.get("file"))
        undeclared = [
            source
            for source in referenced_sources
            if source and source not in declared_sources
        ]
        if undeclared:
            raise ValueError(
                f"{case.get('case_id')} references undeclared runtime source(s): "
                + ", ".join(undeclared)
            )
    return suite


_ZH_DIGITS = "零一二三四五六七八九"
_ZH_NUMERAL_CHARS = "零〇一二兩三四五六七八九十百千萬"


def _zh_cardinal(value: int) -> str:
    """Render the benchmark's bounded non-negative integer values in zh-TW."""
    if value == 0:
        return "零"
    if not 0 < value < 10_000:
        return ""
    units = ((1000, "千"), (100, "百"), (10, "十"))
    parts: list[str] = []
    remainder = value
    pending_zero = False
    for divisor, unit in units:
        digit, remainder = divmod(remainder, divisor)
        if digit:
            if pending_zero:
                parts.append("零")
                pending_zero = False
            if not (divisor == 10 and digit == 1 and not parts):
                parts.append(_ZH_DIGITS[digit])
            parts.append(unit)
        elif parts and remainder:
            pending_zero = True
    if remainder:
        if pending_zero:
            parts.append("零")
        parts.append(_ZH_DIGITS[remainder])
    return "".join(parts)


def _contains_value(text: str, value: object) -> bool:
    rendered = str(value)
    if re.search(rf"(?<!\d){re.escape(rendered)}(?!\d)", text):
        return True
    if isinstance(value, int) and value >= 0:
        variants = {_zh_cardinal(value), "".join(_ZH_DIGITS[int(ch)] for ch in rendered)}
        if value >= 2000:
            variants.add(_zh_cardinal(value).replace("二千", "兩千", 1))
        return any(
            variant
            and re.search(
                rf"(?<![{_ZH_NUMERAL_CHARS}]){re.escape(variant)}(?![{_ZH_NUMERAL_CHARS}])",
                text,
            )
            for variant in variants
        )
    return False


def _literal_semantic_checks(
    expectations: Iterable[dict[str, Any]], target: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    checks: list[dict[str, Any]] = []
    manual: list[dict[str, Any]] = []
    for expectation in expectations:
        kind = str(expectation.get("kind") or "")
        row = {
            "check_id": str(expectation.get("check_id") or ""),
            "kind": kind,
            "description": str(expectation.get("description") or ""),
        }
        if kind == "manual":
            row["status"] = "manual"
            manual.append(row)
            continue
        values = [str(value) for value in expectation.get("values") or []]
        if kind == "required_any_substring":
            passed = any(value in target for value in values)
        elif kind == "required_all_substrings":
            passed = all(value in target for value in values)
        elif kind == "forbidden_substring":
            passed = all(value not in target for value in values)
        else:
            raise ValueError(f"unknown semantic expectation kind: {kind!r}")
        row.update({"passed": passed, "values": values})
        checks.append(row)
    return checks, manual


def score_candidate(
    case: dict[str, Any],
    target_text: str | None,
    *,
    status: str = "success",
    subtitle_emitted: bool | None = None,
) -> dict[str, Any]:
    target = str(target_text or "")
    gates = case.get("hard_gates") or {}
    required = [str(value) for value in gates.get("required_canonical_tokens") or []]
    forbidden = [str(value) for value in gates.get("forbidden_wrong_variants") or []]
    approved = [str(value) for value in gates.get("approved_script_terms") or []]
    numbers = list(gates.get("number_unit_values_to_preserve") or [])
    has_output = bool(target) and status == "success"

    quality = translation_quality(
        str(case.get("source_text") or ""),
        target,
        approved_terms=frozenset(approved),
    )
    classifications = set(quality.get("quality_classifications") or [])
    flags = set(quality.get("quality_flags") or [])
    script_passed = not (
        "target_has_unexpected_hangul" in classifications
        or "target_has_japanese" in flags
        or bool(_KANA_RE.search(target))
    )
    sentence_type = str(gates.get("sentence_type") or "preserve")
    question_passed = sentence_type != "question" or "?" in target or "？" in target
    semantic_checks, manual_semantic = _literal_semantic_checks(
        case.get("semantic_expectations") or (), target
    )
    gate_results = {
        "has_output": has_output,
        "canonical_required": all(token in target for token in required),
        "canonical_forbidden": all(token not in target for token in forbidden),
        "script_compliance": script_passed,
        "number_unit_preservation": all(_contains_value(target, value) for value in numbers),
        "sentence_type": question_passed,
    }
    literal_semantic_passed = all(
        bool(check.get("passed")) for check in semantic_checks
    )
    machine_passed = all(gate_results.values()) and literal_semantic_passed
    return {
        "status": status,
        "subtitle_emitted": subtitle_emitted,
        "target_text": target_text,
        "machine_passed": machine_passed,
        "hard_gates": gate_results,
        "semantic_literal_passed": literal_semantic_passed,
        "semantic_checks": semantic_checks,
        "manual_checks": [
            *manual_semantic,
            *(
                [{"check_id": "roles_and_direction", "status": "manual"}]
                if gates.get("preserve_roles_and_direction")
                else []
            ),
            *(
                [{"check_id": "unsupported_invention", "status": "manual"}]
                if gates.get("no_unsupported_entity_event_fact_invention")
                else []
            ),
        ],
        "quality_flags": sorted(flags),
        "quality_classifications": sorted(classifications),
    }


def deterministic_ownership(case: dict[str, Any], target_text: str | None) -> dict[str, Any]:
    source = str(case.get("source_text") or "")
    target = str(target_text or "")
    with bind_profile_id(str(case.get("profile_id") or "")):
        normalized = _normalize_source_before_matching(source)
        obligations = _resolve_active_canonical_obligations(normalized)
        known_source_spans = tuple(
            span for obligation in obligations for span in obligation.source_spans
        )
        unknown = resolve_unknown_name_escrow(
            normalized, known_source_spans=known_source_spans
        )
        terminology = resolve_semantic_terminology(unknown.provider_source)
        corrected = _apply_source_aware_corrections(normalized, target) if target else target
    return {
        "source_normalization_changed": normalized != source,
        "normalized_source": normalized,
        "unknown_name_escrow_active": unknown.active,
        "unknown_name_terms": list(unknown.approved_hangul_terms),
        "semantic_terminology_active": terminology.active,
        "semantic_terminology_rule_ids": [term.rule_id for term in terminology.terms],
        "target_correction_changed": corrected != target,
        "corrected_target": corrected or None,
    }


def _result_index(path: Path) -> dict[str, dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        rows = payload["results"]
    elif isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = [dict(value, case_id=key) for key, value in payload.items()]
    else:
        raise ValueError("results must be a list, mapping, or {'results': [...]} object")
    indexed = {
        str(row.get("case_id") or ""): row
        for row in rows
        if isinstance(row, dict) and row.get("case_id")
    }
    return indexed


def evaluate_suite(
    suite: dict[str, Any], *, results: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    stage_counts: Counter[str] = Counter()
    stage_passes: Counter[str] = Counter()
    rescue = Counter()
    cases = suite["cases"]
    if results is not None:
        expected = {str(case["case_id"]) for case in cases}
        missing = sorted(expected - set(results))
        extra = sorted(set(results) - expected)
        if missing or extra:
            raise ValueError(f"results case coverage mismatch: missing={missing}, extra={extra}")

    for case in cases:
        case_id = str(case["case_id"])
        observation = (
            results[case_id] if results is not None else case["production_observation"]
        )
        baseline = score_candidate(
            case,
            observation.get("target_text"),
            status=str(observation.get("status") or ""),
            subtitle_emitted=observation.get("subtitle_emitted"),
        )
        stages: dict[str, Any] = {"candidate_or_observation": baseline}
        trace = case.get("production_trace") if results is None else observation.get("production_trace")
        if isinstance(trace, dict):
            first = trace.get("first_provider") or {}
            raw = score_candidate(
                case,
                first.get("raw_output"),
                status="success" if first.get("raw_output") else str(first.get("status") or ""),
            )
            corrected = score_candidate(
                case,
                first.get("corrected_output"),
                status="success" if first.get("corrected_output") else str(first.get("status") or ""),
            )
            final = trace.get("final") or {}
            final_score = score_candidate(
                case,
                final.get("target_text"),
                status=str(final.get("status") or ""),
                subtitle_emitted=final.get("subtitle_emitted"),
            )
            fallback_scores = []
            for attempt in trace.get("fallback_attempts") or []:
                candidate = attempt.get("corrected_output") or attempt.get("raw_output")
                fallback_scores.append(
                    {
                        "engine": attempt.get("engine"),
                        "score": score_candidate(
                            case,
                            candidate,
                            status="success" if candidate else str(attempt.get("status") or ""),
                        ),
                    }
                )
            stages.update(
                {
                    "first_provider_raw": raw,
                    "first_provider_corrected": corrected,
                    "fallback_attempts": fallback_scores,
                    "final": final_score,
                }
            )
            rescue["recorded_correction_changed"] += (
                first.get("raw_output") != first.get("corrected_output")
            )
            rescue["recorded_correction_machine_wins"] += (
                not raw["machine_passed"] and corrected["machine_passed"]
            )
            rescue["fallback_or_final_machine_wins"] += (
                not corrected["machine_passed"] and final_score["machine_passed"]
            )

        stage_counts["candidate_or_observation"] += 1
        stage_passes["candidate_or_observation"] += baseline["machine_passed"]
        rows.append(
            {
                "case_id": case_id,
                "case_role": case.get("case_role"),
                "owning_layer": case.get("owning_layer"),
                "stages": stages,
                "deterministic_ownership": deterministic_ownership(
                    case, observation.get("target_text")
                ),
            }
        )

    return {
        "suite_id": suite.get("suite_id"),
        "case_count": len(cases),
        "mode": "external_results" if results is not None else "production_runtime_baseline",
        "aggregate": {
            "machine_passed": int(stage_passes["candidate_or_observation"]),
            "machine_failed": len(cases) - int(stage_passes["candidate_or_observation"]),
            "semantic_literal_cases": sum(bool(case.get("semantic_expectations")) for case in cases),
            "production_trace_cases": sum(isinstance(case.get("production_trace"), dict) for case in cases),
            "deterministic_rescue": dict(rescue),
        },
        "cases": rows,
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument(
        "--production-runtime-baseline",
        action="store_true",
        help="Score the suite's frozen production observations and traces (offline).",
    )
    parser.add_argument(
        "--results",
        type=Path,
        help="Score a complete externally-produced 75-case result set (offline).",
    )
    parser.add_argument("--output", type=Path, help="Optional JSON report path.")
    args = parser.parse_args()
    if bool(args.results) == bool(args.production_runtime_baseline):
        parser.error("choose exactly one of --results or --production-runtime-baseline")
    suite = load_suite(args.suite)
    results = _result_index(args.results) if args.results else None
    report = evaluate_suite(suite, results=results)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
