from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE_DATA = PROJECT_ROOT / "data" / "translation_profiles.json"
DEFAULT_TEST_FILE = PROJECT_ROOT / "tests" / "test_translation_prompts.py"
SNAPSHOT_CONSTANT = "_TRANSLATION_PROFILE_DATA_HASH"
SNAPSHOT_RE = re.compile(
    rf'({SNAPSHOT_CONSTANT}\s*=\s*)"[0-9a-f]{{64}}"'
)


def canonical_json_hash(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def read_snapshot_hash(test_file: Path = DEFAULT_TEST_FILE) -> str:
    text = test_file.read_text(encoding="utf-8")
    match = SNAPSHOT_RE.search(text)
    if not match:
        raise ValueError(f"{SNAPSHOT_CONSTANT} was not found in {test_file}")
    return match.group(0).split('"')[1]


def update_snapshot_hash(
    profile_data: Path = DEFAULT_PROFILE_DATA,
    test_file: Path = DEFAULT_TEST_FILE,
) -> str:
    new_hash = canonical_json_hash(profile_data)
    text = test_file.read_text(encoding="utf-8")
    updated, count = SNAPSHOT_RE.subn(rf'\1"{new_hash}"', text)
    if count != 1:
        raise ValueError(f"expected exactly one {SNAPSHOT_CONSTANT} in {test_file}, found {count}")
    if updated != text:
        test_file.write_text(updated, encoding="utf-8")
    return new_hash


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Update or verify the translation profile fixture snapshot hash.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the stored hash without modifying test files.",
    )
    parser.add_argument(
        "--profile-data",
        type=Path,
        default=DEFAULT_PROFILE_DATA,
        help="Path to data/translation_profiles.json.",
    )
    parser.add_argument(
        "--test-file",
        type=Path,
        default=DEFAULT_TEST_FILE,
        help="Path to tests/test_translation_prompts.py.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    expected_hash = canonical_json_hash(args.profile_data)

    if args.check:
        stored_hash = read_snapshot_hash(args.test_file)
        if stored_hash != expected_hash:
            print(
                f"{SNAPSHOT_CONSTANT} is stale: stored={stored_hash} expected={expected_hash}",
                file=sys.stderr,
            )
            return 1
        print(f"{SNAPSHOT_CONSTANT} is current: {expected_hash}")
        return 0

    update_snapshot_hash(args.profile_data, args.test_file)
    print(f"{SNAPSHOT_CONSTANT} updated: {expected_hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
