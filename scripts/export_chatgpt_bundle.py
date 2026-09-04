from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.chatgpt_bundle import DEFAULT_MAX_PART_BYTES, export_bundle, list_runs


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export a sanitized portable ChatGPT Project runtime bundle.")
    parser.add_argument("--log-dir", type=Path, default=PROJECT_ROOT / "logs")
    parser.add_argument("--list-runs", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--output-root", type=Path, default=PROJECT_ROOT / "scratch" / "chatgpt_bundles")
    parser.add_argument("--include-audio", action="store_true")
    parser.add_argument("--audio-root", type=Path, default=PROJECT_ROOT / "logs" / "audio_dump")
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "logs" / "live_translate_config.json")
    parser.add_argument("--max-part-bytes", type=int, default=DEFAULT_MAX_PART_BYTES)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.list_runs:
            result = list_runs(args.log_dir)
        else:
            if not args.run_id:
                raise ValueError("--run-id is required unless --list-runs is used")
            result = export_bundle(
                run_id=args.run_id,
                log_dir=args.log_dir,
                output_root=args.output_root,
                project_root=PROJECT_ROOT,
                config_path=args.config,
                audio_root=args.audio_root,
                include_audio=args.include_audio,
                max_part_bytes=args.max_part_bytes,
            )
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
