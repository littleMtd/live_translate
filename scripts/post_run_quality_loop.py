"""Run the post-session translation quality loop.

This wraps the existing offline tools into one command:

1. analyze runtime events into JSON
2. mine correction candidates into Markdown
3. replay deterministic layers and accept snapshot updates

Example:
  python scripts/post_run_quality_loop.py --events logs/runtime_events_20260707.jsonl
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "scratch" / "analysis" / "post_run_quality"
DEFAULT_REPLAY_SNAPSHOT = PROJECT_ROOT / "data" / "replay_eval_snapshot.jsonl"


def _default_events() -> str:
    today = dt.datetime.now().strftime("%Y%m%d")
    return str(PROJECT_ROOT / "logs" / f"runtime_events_{today}.jsonl")


def _run(command: list[str]) -> int:
    print("+ " + " ".join(command), flush=True)
    completed = subprocess.run(command, cwd=PROJECT_ROOT)
    return completed.returncode


def _run_capture_json(command: list[str], output: Path) -> int:
    print("+ " + " ".join(command) + f" > {output}", flush=True)
    with output.open("w", encoding="utf-8") as handle:
        completed = subprocess.run(command, cwd=PROJECT_ROOT, stdout=handle)
    return completed.returncode


def _resolve_event_inputs(event_args: list[str]) -> list[Path]:
    paths: list[Path] = []
    for raw in event_args:
        pattern = str(PROJECT_ROOT / raw) if not Path(raw).is_absolute() else raw
        matches = sorted(glob.glob(pattern))
        if matches:
            paths.extend(Path(match) for match in matches)
        else:
            path = Path(raw)
            paths.append(path if path.is_absolute() else PROJECT_ROOT / path)
    return paths


def _prepare_event_input(event_args: list[str], output_dir: Path) -> Path:
    paths = _resolve_event_inputs(event_args)
    if len(paths) == 1:
        return paths[0]

    combined = output_dir / "runtime_events_combined.jsonl"
    with combined.open("w", encoding="utf-8") as handle:
        for path in paths:
            try:
                with path.open("r", encoding="utf-8") as source:
                    for line in source:
                        handle.write(line)
                        if line and not line.endswith("\n"):
                            handle.write("\n")
            except OSError:
                print(f"warning: event file unavailable: {path}", file=sys.stderr)
    return combined


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--events",
        nargs="+",
        default=[_default_events()],
        help="runtime event JSONL file(s) or glob(s)",
    )
    parser.add_argument("--run-id", action="append", help="limit suggestions to run_id")
    parser.add_argument("--min-count", type=int, default=2)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--snapshot", default=str(DEFAULT_REPLAY_SNAPSHOT))
    parser.add_argument(
        "--skip-replay-update",
        action="store_true",
        help="only analyze/suggest; do not rewrite replay snapshot",
    )
    args = parser.parse_args(argv)

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path(args.output_dir) / stamp
    output_dir.mkdir(parents=True, exist_ok=True)

    python = sys.executable
    event_input = _prepare_event_input(args.events, output_dir)
    analyze_out = output_dir / "runtime_report.json"
    suggest_out = output_dir / "suggestions.md"
    candidate_out = output_dir / "glossary_candidates.json"

    status = _run_capture_json(
        [
            python,
            "scripts/analyze_runtime_events.py",
            "--events",
            str(event_input),
            "--json",
        ],
        analyze_out,
    )
    if status != 0:
        return status

    suggest_cmd = [
        python,
        "scripts/suggest_corrections.py",
        "--events",
        str(event_input),
        "--min-count",
        str(args.min_count),
        "--output",
        str(suggest_out),
        "--json-output",
        str(candidate_out),
    ]
    for run_id in args.run_id or []:
        suggest_cmd.extend(["--run-id", run_id])
    status = _run(suggest_cmd)
    if status != 0:
        return status

    if not args.skip_replay_update:
        status = _run(
            [
                python,
                "scripts/replay_eval.py",
                "run",
                "--snapshot",
                args.snapshot,
                "--update",
            ]
        )
        if status != 0:
            return status

    print(f"post-run quality artifacts: {output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
