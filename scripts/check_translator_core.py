from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.evaluate_translations import evaluate_cases, load_eval_cases, summarize
from scripts.update_translation_profile_snapshot import (
    canonical_json_hash,
    read_snapshot_hash,
)


JSON_FIXTURES = [
    PROJECT_ROOT / "data" / "default_slang.json",
    PROJECT_ROOT / "data" / "streamer_profiles.json",
    PROJECT_ROOT / "data" / "translation_corrections.json",
    PROJECT_ROOT / "data" / "translation_profiles.json",
    PROJECT_ROOT / "data" / "eval_cases.json",
]
FOCUSED_TESTS = [
    "tests/test_config.py",
    "tests/test_streamer_profiles.py",
    "tests/test_activity_context.py",
    "tests/test_translation_prompts.py",
    "tests/test_evaluate_translations.py",
    "tests/test_analyze_cache.py",
    "tests/test_metrics.py",
    "tests/test_translation_memory.py",
    "tests/test_translation_runtime.py",
    "tests/test_translation_corrections.py",
    "tests/test_translator.py",
]

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    message: str


def check_json_fixtures(paths: list[Path] = JSON_FIXTURES) -> CheckResult:
    try:
        for path in paths:
            json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return CheckResult("json_fixtures", False, str(exc))
    return CheckResult("json_fixtures", True, f"{len(paths)} JSON fixtures valid")


def check_translation_profile_snapshot() -> CheckResult:
    data_path = PROJECT_ROOT / "data" / "translation_profiles.json"
    test_path = PROJECT_ROOT / "tests" / "test_translation_prompts.py"
    expected_hash = canonical_json_hash(data_path)
    stored_hash = read_snapshot_hash(test_path)
    if expected_hash != stored_hash:
        return CheckResult(
            "translation_profile_snapshot",
            False,
            f"stored={stored_hash} expected={expected_hash}",
        )
    return CheckResult("translation_profile_snapshot", True, expected_hash)


def check_eval_cases() -> CheckResult:
    cases = load_eval_cases(PROJECT_ROOT / "data" / "eval_cases.json")
    summary = summarize(evaluate_cases(cases))
    if summary["failed"]:
        return CheckResult("eval_cases", False, f"failed_ids={summary['failed_ids']}")
    return CheckResult("eval_cases", True, f"{summary['passed']}/{summary['total']} passed")


def run_focused_pytest() -> CheckResult:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *FOCUSED_TESTS,
            "-q",
        ],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        output = (completed.stdout + "\n" + completed.stderr).strip()
        return CheckResult("focused_pytest", False, output[-2000:])
    last_line = completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else "pytest passed"
    return CheckResult("focused_pytest", True, last_line)


def run_checks(include_pytest: bool = True) -> list[CheckResult]:
    results = [
        check_json_fixtures(),
        check_translation_profile_snapshot(),
        check_eval_cases(),
    ]
    if include_pytest:
        results.append(run_focused_pytest())
    return results


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run core translator maintenance checks.")
    parser.add_argument(
        "--skip-pytest",
        action="store_true",
        help="Skip focused pytest checks and run only fast fixture/eval checks.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    results = run_checks(include_pytest=not args.skip_pytest)
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        print(f"- {status} {result.name}: {result.message}")
    return 0 if all(result.passed for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
