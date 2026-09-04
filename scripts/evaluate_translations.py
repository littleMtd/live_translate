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
    expected_empty: bool = False
    min_term_counts: tuple[tuple[str, int], ...] = ()
    recoverability: str = "translatable"
    dimensions: tuple[str, ...] = ()
    requires_activity: bool = False
    requires_audio: bool = False
    current_output: str | None = None


@dataclass(frozen=True)
class CaseEvaluation:
    case_id: str
    passed: bool
    failures: tuple[str, ...]
    output: str
    recoverability: str = "translatable"
    dimensions: tuple[str, ...] = ()
    requires_activity: bool = False
    requires_audio: bool = False


_RECOVERABILITY = frozenset(
    {
        "translatable",
        "currently_mistranslated",
        "partially_recoverable",
        "stt_unrecoverable",
    }
)
_QUALITY_DIMENSIONS = frozenset(
    {"faithfulness", "hallucination", "entity", "context", "role", "domain", "noise"}
)


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


def _bool_value(value: Any, field_name: str, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be boolean")
    return value


def _term_counts(value: Any, field_name: str) -> tuple[tuple[str, int], ...]:
    if value is None:
        return ()
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be an object")
    result: list[tuple[str, int]] = []
    for term, count in value.items():
        if (
            not isinstance(term, str)
            or not term
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 1
        ):
            raise ValueError(
                f"{field_name} must map non-empty strings to positive integers"
            )
        result.append((term, count))
    return tuple(result)


def _load_runtime_sources(
    suite_path: Path,
    artifacts: list[str],
) -> dict[tuple[str, int], str]:
    sources: dict[tuple[str, int], str] = {}
    for artifact in artifacts:
        raw_path = Path(artifact)
        candidates = (
            (raw_path,) if raw_path.is_absolute() else (
                suite_path.parent / raw_path,
                suite_path.parent.parent / raw_path,
                Path.cwd() / raw_path,
            )
        )
        artifact_path = next((item for item in candidates if item.is_file()), None)
        if artifact_path is None:
            raise ValueError(f"source artifact not found: {artifact}")
        for line_number, line in enumerate(
            artifact_path.read_text(encoding="utf-8").splitlines(),
            1,
        ):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"invalid JSON in {artifact}:{line_number}"
                ) from exc
            if event.get("event_type") != "translation":
                continue
            run_id = event.get("run_id")
            sequence_id = event.get("sequence_id")
            source_text = event.get("source_text")
            if (
                not isinstance(run_id, str)
                or isinstance(sequence_id, bool)
                or not isinstance(sequence_id, int)
                or not isinstance(source_text, str)
            ):
                continue
            key = (run_id, sequence_id)
            previous = sources.get(key)
            if previous is not None and previous != source_text:
                raise ValueError(f"conflicting runtime source for {run_id}:{sequence_id}")
            sources[key] = source_text
    return sources


def load_eval_cases(path: Path = DEFAULT_CASES_PATH) -> list[EvalCase]:
    data = json.loads(path.read_text(encoding="utf-8"))
    excluded_runs: set[str] = set()
    provenance_policy = ""
    runtime_sources: dict[tuple[str, int], str] = {}
    if isinstance(data, dict):
        provenance_policy = str(data.get("provenance_policy", "") or "")
        if provenance_policy:
            if provenance_policy != "natural_runtime_and_derived_policy":
                raise ValueError("unsupported provenance_policy")
            if data.get("schema_version") != 1:
                raise ValueError("provenance suites require schema_version 1")
            if not isinstance(data.get("dataset_id"), str) or not data["dataset_id"].strip():
                raise ValueError("provenance suites require dataset_id")
            artifacts = data.get("source_artifacts")
            if (
                not isinstance(artifacts, list)
                or not artifacts
                or not all(isinstance(item, str) and item.strip() for item in artifacts)
            ):
                raise ValueError("provenance suites require source_artifacts")
            runtime_sources = _load_runtime_sources(path, artifacts)
        raw_exclusions = data.get("excluded_runs", [])
        if not isinstance(raw_exclusions, list):
            raise ValueError("excluded_runs must be a list")
        for index, exclusion in enumerate(raw_exclusions):
            if not isinstance(exclusion, dict) or not isinstance(
                exclusion.get("run_id"), str
            ):
                raise ValueError(f"excluded_runs[{index}] must contain run_id")
            excluded_runs.add(exclusion["run_id"])
        data = data.get("cases")
    if not isinstance(data, list):
        raise ValueError("eval cases must be a JSON list or suite object")

    cases: list[EvalCase] = []
    seen_ids: set[str] = set()
    for index, raw_case in enumerate(data):
        if not isinstance(raw_case, dict):
            raise ValueError(f"cases[{index}] must be an object")

        case_id = raw_case.get("id")
        source_text = raw_case.get("source_text")
        reference_output = raw_case.get("reference_output")
        expected_empty = _bool_value(
            raw_case.get("expected_empty"),
            f"cases[{index}].expected_empty",
        )
        if not isinstance(case_id, str) or not case_id:
            raise ValueError(f"cases[{index}].id must be a non-empty string")
        if case_id in seen_ids:
            raise ValueError(f"duplicate eval case id: {case_id}")
        runtime_ref = raw_case.get("runtime_ref")
        derived_ref = raw_case.get("derived_from_runtime_ref")
        if provenance_policy and (runtime_ref is None) == (derived_ref is None):
            raise ValueError(
                f"cases[{index}] must contain exactly one of runtime_ref or "
                "derived_from_runtime_ref"
            )
        if derived_ref is not None:
            if (
                not isinstance(derived_ref, dict)
                or not isinstance(derived_ref.get("run_id"), str)
                or isinstance(derived_ref.get("sequence_id"), bool)
                or not isinstance(derived_ref.get("sequence_id"), int)
                or not isinstance(derived_ref.get("transformation"), str)
                or not derived_ref["transformation"].strip()
            ):
                raise ValueError(
                    f"cases[{index}].derived_from_runtime_ref must contain "
                    "run_id, sequence_id, and transformation"
                )
            if derived_ref["run_id"] in excluded_runs:
                raise ValueError(
                    f"cases[{index}] derives from excluded run {derived_ref['run_id']}"
                )
            derived_key = (derived_ref["run_id"], derived_ref["sequence_id"])
            if provenance_policy and derived_key not in runtime_sources:
                raise ValueError(
                    f"cases[{index}] derived runtime_ref was not found"
                )
        if runtime_ref is not None:
            if (
                not isinstance(runtime_ref, dict)
                or not isinstance(runtime_ref.get("run_id"), str)
                or isinstance(runtime_ref.get("sequence_id"), bool)
                or not isinstance(runtime_ref.get("sequence_id"), int)
            ):
                raise ValueError(
                    f"cases[{index}].runtime_ref must contain run_id and sequence_id"
                )
            if runtime_ref["run_id"] in excluded_runs:
                raise ValueError(
                    f"cases[{index}] references excluded run {runtime_ref['run_id']}"
                )
        if not isinstance(source_text, str) or not source_text.strip():
            raise ValueError(f"cases[{index}].source_text must be a non-empty string")
        if provenance_policy and runtime_ref is not None:
            runtime_key = (runtime_ref["run_id"], runtime_ref["sequence_id"])
            if runtime_key not in runtime_sources:
                raise ValueError(f"cases[{index}].runtime_ref was not found")
            if runtime_sources[runtime_key] != source_text:
                raise ValueError(
                    f"cases[{index}].source_text does not match runtime_ref"
                )
        if not isinstance(reference_output, str):
            raise ValueError(f"cases[{index}].reference_output must be a string")
        if expected_empty:
            if reference_output.strip():
                raise ValueError(
                    f"cases[{index}].reference_output must be empty when expected_empty is true"
                )
        elif not reference_output.strip():
            raise ValueError(f"cases[{index}].reference_output must be a non-empty string")
        recoverability = str(raw_case.get("recoverability", "translatable"))
        if recoverability not in _RECOVERABILITY:
            raise ValueError(
                f"cases[{index}].recoverability must be one of {sorted(_RECOVERABILITY)}"
            )
        if expected_empty and recoverability != "stt_unrecoverable":
            raise ValueError(
                f"cases[{index}] expected_empty requires stt_unrecoverable"
            )
        dimensions = _string_tuple(
            raw_case.get("dimensions"),
            f"cases[{index}].dimensions",
        )
        unknown_dimensions = set(dimensions) - _QUALITY_DIMENSIONS
        if unknown_dimensions:
            raise ValueError(
                f"cases[{index}].dimensions contains unknown values: "
                f"{sorted(unknown_dimensions)}"
            )
        current_output = raw_case.get("current_output")
        if current_output is not None and not isinstance(current_output, str):
            raise ValueError(f"cases[{index}].current_output must be a string")

        seen_ids.add(case_id)
        cases.append(
            EvalCase(
                case_id=case_id,
                source_text=source_text,
                reference_output=reference_output,
                incomplete=_bool_value(
                    raw_case.get("incomplete"),
                    f"cases[{index}].incomplete",
                ),
                categories=_string_tuple(raw_case.get("categories"), f"cases[{index}].categories"),
                note=str(raw_case.get("note", "")),
                expected_terms=_string_tuple(raw_case.get("expected_terms"), f"cases[{index}].expected_terms"),
                forbidden_terms=_string_tuple(raw_case.get("forbidden_terms"), f"cases[{index}].forbidden_terms"),
                max_korean_ratio=_float_value(raw_case.get("max_korean_ratio"), f"cases[{index}].max_korean_ratio", 0.2),
                max_japanese_chars=_int_value(raw_case.get("max_japanese_chars"), f"cases[{index}].max_japanese_chars", 2),
                max_output_ratio=_float_value(raw_case.get("max_output_ratio"), f"cases[{index}].max_output_ratio", 3.0),
                expected_empty=expected_empty,
                min_term_counts=_term_counts(
                    raw_case.get("min_term_counts"),
                    f"cases[{index}].min_term_counts",
                ),
                recoverability=recoverability,
                dimensions=dimensions,
                requires_activity=_bool_value(
                    raw_case.get("requires_activity"),
                    f"cases[{index}].requires_activity",
                ),
                requires_audio=_bool_value(
                    raw_case.get("requires_audio"),
                    f"cases[{index}].requires_audio",
                ),
                current_output=current_output,
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

    if case.expected_empty:
        if normalized_output:
            failures.append("expected_empty")
        return CaseEvaluation(
            case_id=case.case_id,
            passed=not failures,
            failures=tuple(failures),
            output=normalized_output,
            recoverability=case.recoverability,
            dimensions=case.dimensions,
            requires_activity=case.requires_activity,
            requires_audio=case.requires_audio,
        )

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

    for term, minimum in case.min_term_counts:
        actual = normalized_output.count(term)
        if actual < minimum:
            failures.append(f"term_count<{minimum}:{term}:{actual}")

    return CaseEvaluation(
        case_id=case.case_id,
        passed=not failures,
        failures=tuple(failures),
        output=normalized_output,
        recoverability=case.recoverability,
        dimensions=case.dimensions,
        requires_activity=case.requires_activity,
        requires_audio=case.requires_audio,
    )


def evaluate_cases(cases: list[EvalCase], outputs: dict[str, str] | None = None) -> list[CaseEvaluation]:
    selected_outputs = (
        {case.case_id: case.reference_output for case in cases}
        if outputs is None
        else outputs
    )
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
                    recoverability=case.recoverability,
                    dimensions=case.dimensions,
                    requires_activity=case.requires_activity,
                    requires_audio=case.requires_audio,
                )
            )
            continue
        results.append(evaluate_case(case, output))
    return results


def summarize(results: list[CaseEvaluation]) -> dict[str, Any]:
    failed = [result for result in results if not result.passed]
    grouped: dict[str, dict[str, int]] = {}
    for result in results:
        labels = [f"recoverability:{result.recoverability}"]
        labels.extend(f"dimension:{item}" for item in result.dimensions)
        if result.requires_activity:
            labels.append("requirement:activity")
        if result.requires_audio:
            labels.append("requirement:audio")
        for label in labels:
            bucket = grouped.setdefault(label, {"total": 0, "passed": 0, "failed": 0})
            bucket["total"] += 1
            bucket["passed" if result.passed else "failed"] += 1
    return {
        "total": len(results),
        "passed": len(results) - len(failed),
        "failed": len(failed),
        "failed_ids": [result.case_id for result in failed],
        "groups": grouped,
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
        "--use-current-output",
        action="store_true",
        help="Evaluate each case's embedded current_output baseline.",
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
    if args.results and args.use_current_output:
        raise ValueError("choose --results or --use-current-output, not both")
    outputs = load_outputs(args.results) if args.results else None
    if args.use_current_output:
        outputs = {
            case.case_id: case.current_output
            for case in cases
            if case.current_output is not None
        }
    results = evaluate_cases(cases, outputs)
    summary = summarize(results)
    if args.json:
        print(json.dumps({"summary": summary, "results": [result.__dict__ for result in results]}, ensure_ascii=False, indent=2))
    else:
        _print_report(results)
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
