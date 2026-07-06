"""Offline replay-eval harness for the deterministic translation layers.

Purpose (harness engineering): before shipping a change to corrections /
glossary / policy filters / name rendering, replay a frozen golden set of
real logged sentences through the *deterministic* pipeline layers and diff
the outcomes against the snapshot. No API calls, fully deterministic,
seconds to run — "這個改動讓幾句變好、幾句變壞" before it ships.

What is replayed per case (model output held fixed = the shipped target):
  1. policy   — TranslationPolicy.rejection_reason(source): would this
                sentence be filtered, and for which reason?
  2. norm     — _normalize_source_before_matching(source): profile-scoped
                STT source normalization.
  3. target   — _apply_source_aware_corrections(source, shipped_target):
                name rendering / source-aware replacements applied to the
                previously shipped output. Idempotent under the baseline
                ruleset, so any difference is exactly the blast radius of
                a rule/data change.

Usage:
  # 1) freeze a golden set from real runtime events (records expectations
  #    computed under the CURRENT code+data):
  python scripts/replay_eval.py build --events "logs/runtime_events_2026*.jsonl" \
      --output data/replay_eval_snapshot.jsonl

  # 2) after editing corrections/glossary/policy — see what changed:
  python scripts/replay_eval.py run --snapshot data/replay_eval_snapshot.jsonl

  # 3) accept intentional changes (refresh expectations in place):
  python scripts/replay_eval.py run --snapshot data/replay_eval_snapshot.jsonl --update

Exit code: run returns 1 when any case diverges from the snapshot (CI-able),
0 otherwise. build/update always return 0.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import sys
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import cfg  # noqa: E402
from modules.translator import (  # noqa: E402
    _apply_source_aware_corrections,
    _normalize_source_before_matching,
    _new_translation_policy,
)

DEFAULT_SNAPSHOT = PROJECT_ROOT / "data" / "replay_eval_snapshot.jsonl"


@contextmanager
def _active_profile(profile_id: str, use_profile: bool = True):
    original_profile = cfg.translation.streamer_profile
    original_use = cfg.translation.use_profile
    object.__setattr__(cfg.translation, "streamer_profile", profile_id)
    object.__setattr__(cfg.translation, "use_profile", use_profile)
    try:
        yield
    finally:
        object.__setattr__(cfg.translation, "streamer_profile", original_profile)
        object.__setattr__(cfg.translation, "use_profile", original_use)


def evaluate_case(source: str, shipped_target: str, profile_id: str) -> dict:
    """Run the deterministic layers for one case. Fresh policy per case so
    stateful checks (duplicate/last_input) cannot couple cases together."""
    with _active_profile(profile_id):
        policy = _new_translation_policy()
        rejection = policy.rejection_reason(source)
        norm = _normalize_source_before_matching(source)
        target = _apply_source_aware_corrections(source, shipped_target) if shipped_target else ""
    return {
        "expect_rejection": rejection,
        "expect_norm_source": norm,
        "expect_target": target,
    }


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------

def iter_translation_events(patterns: list[str]):
    for pattern in patterns:
        for path in sorted(glob.glob(pattern)):
            day = Path(path).stem.replace("runtime_events_", "")
            with open(path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("event_type") != "translation":
                        continue
                    if event.get("engine") == "mock":  # test pollution guard
                        continue
                    event["_day"] = day
                    yield event


def case_id(source: str, profile_id: str) -> str:
    return hashlib.sha256(f"{profile_id}\x00{source}".encode("utf-8")).hexdigest()[:16]


def build(args) -> int:
    events = list(iter_translation_events(args.events))
    if not events:
        print("no translation events matched — check --events globs", file=sys.stderr)
        return 1

    # Stratify: spread cases across (profile, day), dedupe identical sources,
    # keep filtered events too (policy regressions are exactly about those).
    buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    seen: set[str] = set()
    for event in events:
        source = (event.get("source_text") or "").strip()
        if not source or len(source) > args.max_chars:
            continue
        profile_id = event.get("profile_id") or ""
        cid = case_id(source, profile_id)
        if cid in seen:
            continue
        seen.add(cid)
        buckets[(profile_id, event["_day"])].append(event)

    cases: list[dict] = []
    per_profile: Counter = Counter()
    # round-robin over (profile, day) buckets for even coverage
    bucket_lists = sorted(buckets.items())
    index = 0
    while bucket_lists:
        remaining = []
        for (profile_id, day), bucket in bucket_lists:
            if per_profile[profile_id] >= args.per_profile:
                continue
            if index < len(bucket):
                event = bucket[index]
                source = (event.get("source_text") or "").strip()
                shipped_target = event.get("target_text") or ""
                expectations = evaluate_case(source, shipped_target, profile_id)
                cases.append({
                    "case_id": case_id(source, profile_id),
                    "profile_id": profile_id,
                    "day": day,
                    "source": source,
                    "shipped_status": event.get("status"),
                    "shipped_filter_reason": event.get("filter_reason") or "",
                    "shipped_target": shipped_target,
                    **expectations,
                })
                per_profile[profile_id] += 1
            if index + 1 < len(bucket) and per_profile[profile_id] < args.per_profile:
                remaining.append(((profile_id, day), bucket))
        bucket_lists = remaining
        index += 1

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as handle:
        for case in cases:
            handle.write(json.dumps(case, ensure_ascii=False) + "\n")

    drift = sum(1 for c in cases if c["shipped_target"] and c["expect_target"] != c["shipped_target"])
    print(f"snapshot written: {out} ({len(cases)} cases)")
    print("per profile:", dict(per_profile))
    print(f"historical drift (shipped output would render differently under "
          f"current rules): {drift}")
    return 0


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

def run(args) -> int:
    snapshot_path = Path(args.snapshot)
    cases = [json.loads(line) for line in open(snapshot_path, encoding="utf-8")
             if line.strip()]
    diffs: list[tuple[str, dict, dict]] = []
    for case in cases:
        current = evaluate_case(case["source"], case["shipped_target"], case["profile_id"])
        kinds = []
        if current["expect_rejection"] != case["expect_rejection"]:
            kinds.append("POLICY")
        if current["expect_norm_source"] != case["expect_norm_source"]:
            kinds.append("NORM")
        if current["expect_target"] != case["expect_target"]:
            kinds.append("TARGET")
        if kinds:
            diffs.append(("+".join(kinds), case, current))
            if args.update:
                case.update(current)

    print(f"replayed {len(cases)} cases — {len(diffs)} diverge from snapshot")
    summary = Counter(kind for kind, _, _ in diffs)
    if summary:
        print("by kind:", dict(summary))
    shown = 0
    for kind, case, current in diffs:
        if shown >= args.max_show:
            print(f"... ({len(diffs) - shown} more)")
            break
        shown += 1
        print(f"\n[{kind}] {case['case_id']} profile={case['profile_id']} day={case['day']}")
        print(f"  source: {case['source'][:80]}")
        if "POLICY" in kind:
            print(f"  rejection: {case['expect_rejection']!r} -> {current['expect_rejection']!r}")
        if "NORM" in kind:
            print(f"  norm: {case['expect_norm_source'][:60]!r} -> {current['expect_norm_source'][:60]!r}")
        if "TARGET" in kind:
            print(f"  target: {case['expect_target'][:60]!r} -> {current['expect_target'][:60]!r}")

    if args.update and diffs:
        with open(snapshot_path, "w", encoding="utf-8") as handle:
            for case in cases:
                handle.write(json.dumps(case, ensure_ascii=False) + "\n")
        print(f"\nsnapshot updated in place: {snapshot_path}")
        return 0
    return 1 if diffs else 0


def main(argv: list[str] | None = None) -> int:
    # Korean sources must survive Windows cp950 consoles. reconfigure() keeps
    # the same stream object, so pytest's capture stays intact.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="freeze a golden set from runtime events")
    p_build.add_argument("--events", nargs="+", required=True,
                         help="glob(s) of runtime_events_*.jsonl")
    p_build.add_argument("--output", default=str(DEFAULT_SNAPSHOT))
    p_build.add_argument("--per-profile", type=int, default=150)
    p_build.add_argument("--max-chars", type=int, default=300)
    p_build.set_defaults(func=build)

    p_run = sub.add_parser("run", help="replay the golden set and diff")
    p_run.add_argument("--snapshot", default=str(DEFAULT_SNAPSHOT))
    p_run.add_argument("--update", action="store_true",
                       help="accept diffs: rewrite expectations in place")
    p_run.add_argument("--max-show", type=int, default=20)
    p_run.set_defaults(func=run)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
