from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES_PATH = PROJECT_ROOT / "data" / "eval_cases.json"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    source_text: str
    reference_output: str
    incomplete: bool
    categories: tuple[str, ...]
    note: str
    expected_terms: tuple[str, ...]
    forbidden_terms: tuple[str, ...]
    max_korean_ratio: float
    max_japanese_chars: int
    max_output_ratio: float


@dataclass(frozen=True)
class CaseEvaluation:
    case_id: str
    passed: bool
    failures: tuple[str, ...]
    output: str


def _string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be a list of strings")
    return tuple(value)


def _float_value(value: Any, field_name: str, default: float) -> float:
    if value is None:
        return default
    if not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a number")
    return float(value)


def _int_value(value: Any, field_name: str, default: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    return value


def load_eval_cases(path: Path = DEFAULT_CASES_PATH) -> list[EvalCase]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("eval cases must be a JSON list")

    cases: list[EvalCase] = []
    seen_ids: set[str] = set()
    for index, raw_case in enumerate(data):
        if not isinstance(raw_case, dict):
            raise ValueError(f"cases[{index}] must be an object")

        case_id = raw_case.get("id")
        source_text = raw_case.get("source_text")
        reference_output = raw_case.get("reference_output")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError(f"cases[{index}].id must be a non-empty string")
        if case_id in seen_ids:
            raise ValueError(f"duplicate eval case id: {case_id}")
        if not isinstance(source_text, str) or not source_text.strip():
            raise ValueError(f"cases[{index}].source_text must be a non-empty string")
        if not isinstance(reference_output, str) or not reference_output.strip():
            raise ValueError(f"cases[{index}].reference_output must be a non-empty string")

        seen_ids.add(case_id)
        cases.append(
            EvalCase(
                case_id=case_id,
                source_text=source_text,
                reference_output=reference_output,
                incomplete=bool(raw_case.get("incomplete", False)),
                categories=_string_tuple(raw_case.get("categories"), f"cases[{index}].categories"),
                note=str(raw_case.get("note", "")),
                expected_terms=_string_tuple(raw_case.get("expected_terms"), f"cases[{index}].expected_terms"),
                forbidden_terms=_string_tuple(raw_case.get("forbidden_terms"), f"cases[{index}].forbidden_terms"),
                max_korean_ratio=_float_value(raw_case.get("max_korean_ratio"), f"cases[{index}].max_korean_ratio", 0.2),
                max_japanese_chars=_int_value(raw_case.get("max_japanese_chars"), f"cases[{index}].max_japanese_chars", 2),
                max_output_ratio=_float_value(raw_case.get("max_output_ratio"), f"cases[{index}].max_output_ratio", 3.0),
            )
        )
    return cases


def load_outputs(path: Path) -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return {str(case_id): _extract_output(value, str(case_id)) for case_id, value in data.items()}
    if isinstance(data, list):
        outputs: dict[str, str] = {}
        for index, item in enumerate(data):
            if not isinstance(item, dict):
                raise ValueError(f"results[{index}] must be an object")
            case_id = item.get("id") or item.get("case_id")
            if not isinstance(case_id, str) or not case_id:
                raise ValueError(f"results[{index}].id must be a non-empty string")
            outputs[case_id] = _extract_output(item, case_id)
        return outputs
    raise ValueError("results must be either an object or a list")


def _extract_output(value: Any, case_id: str) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        output = value.get("output")
        if output is None:
            output = value.get("translation")
        if output is None:
            output = value.get("text")
        if isinstance(output, str):
            return output
    raise ValueError(f"result for {case_id!r} must contain a string output")


def evaluate_case(case: EvalCase, output: str) -> CaseEvaluation:
    normalized_output = output.strip()
    failures: list[str] = []

    if not normalized_output:
        failures.append("empty_output")
    if normalized_output == case.source_text.strip():
        failures.append("identical_to_source")

    korean_ratio = _korean_ratio(normalized_output)
    if korean_ratio > case.max_korean_ratio:
        failures.append(f"korean_ratio>{case.max_korean_ratio:g}:{korean_ratio:.2f}")

    japanese_chars = _japanese_chars(normalized_output)
    if japanese_chars > case.max_japanese_chars:
        failures.append(f"japanese_chars>{case.max_japanese_chars}:{japanese_chars}")

    output_chars = _non_space_len(normalized_output)
    source_chars = max(_non_space_len(case.source_text), 1)
    output_ratio = output_chars / source_chars
    if output_ratio > case.max_output_ratio:
        failures.append(f"output_ratio>{case.max_output_ratio:g}:{output_ratio:.2f}")

    for term in case.expected_terms:
        if term and term not in normalized_output:
            failures.append(f"missing_expected:{term}")

    for term in case.forbidden_terms:
        if term and term in normalized_output:
            failures.append(f"forbidden_term:{term}")

    return CaseEvaluation(
        case_id=case.case_id,
        passed=not failures,
        failures=tuple(failures),
        output=normalized_output,
    )


def evaluate_cases(cases: list[EvalCase], outputs: dict[str, str] | None = None) -> list[CaseEvaluation]:
    selected_outputs = outputs or {case.case_id: case.reference_output for case in cases}
    results: list[CaseEvaluation] = []
    for case in cases:
        output = selected_outputs.get(case.case_id)
        if output is None:
            results.append(
                CaseEvaluation(
                    case_id=case.case_id,
                    passed=False,
                    failures=("missing_output",),
                    output="",
                )
            )
            continue
        results.append(evaluate_case(case, output))
    return results


def summarize(results: list[CaseEvaluation]) -> dict[str, Any]:
    failed = [result for result in results if not result.passed]
    return {
        "total": len(results),
        "passed": len(results) - len(failed),
        "failed": len(failed),
        "failed_ids": [result.case_id for result in failed],
    }


def _korean_ratio(text: str) -> float:
    chars = [char for char in text if not char.isspace()]
    if not chars:
        return 0.0
    korean = sum(1 for char in chars if "\uac00" <= char <= "\ud7a3")
    return korean / len(chars)


def _japanese_chars(text: str) -> int:
    return sum(
        1
        for char in text
        if ("\u3040" <= char <= "\u309f") or ("\u30a0" <= char <= "\u30ff")
    )


def _non_space_len(text: str) -> int:
    return sum(1 for char in text if not char.isspace())


def _print_report(results: list[CaseEvaluation]) -> None:
    summary = summarize(results)
    print(
        f"Translation eval: {summary['passed']}/{summary['total']} passed; "
        f"{summary['failed']} failed"
    )
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        print(f"- {status} {result.case_id}")
        for failure in result.failures:
            print(f"  - {failure}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run offline translation quality checks.")
    parser.add_argument(
        "--cases",
        type=Path,
        default=DEFAULT_CASES_PATH,
        help="Path to data/eval_cases.json.",
    )
    parser.add_argument(
        "--results",
        type=Path,
        help="Optional JSON outputs to evaluate. Defaults to each case reference_output.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable summary JSON.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    cases = load_eval_cases(args.cases)
    outputs = load_outputs(args.results) if args.results else None
    results = evaluate_cases(cases, outputs)
    summary = summarize(results)
    if args.json:
        print(json.dumps({"summary": summary, "results": [result.__dict__ for result in results]}, ensure_ascii=False, indent=2))
    else:
        _print_report(results)
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
