from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.forensics_analysis import analyze_forensics_bundle, render_forensics_report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate and trace a schema-v6 runtime bundle.")
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--report-output", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = analyze_forensics_bundle(args.bundle)
    rendered = render_forensics_report(report)
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.report_output:
        args.report_output.parent.mkdir(parents=True, exist_ok=True)
        args.report_output.write_text(rendered, encoding="utf-8")
    if not args.json_output and not args.report_output:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print(rendered, file=sys.stderr, end="")
    return 0 if report["integrity_ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
