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
  4. capsule  — the per-request entity mapping block the source activates
                (the name part of the provider prompt).
  5. guard    — every logged provider candidate re-adjudicated by the
                publication guard, then the copied-name rescue, in route
                order: which candidate would publish now (deepseek / groq /
                rescue / lost) and with what text. Every sentence that had a
                rejected candidate is frozen, so a guard or name-data change
                shows each sentence it gains or loses.

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

from types import SimpleNamespace  # noqa: E402

from config import cfg  # noqa: E402
from modules.translation_guard import (  # noqa: E402
    _adjudicate_translation_candidate,
    _choose_guard_rescue,
)
from modules.translation_normalization import _resolve_entity_request_context  # noqa: E402
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


def _publication(source: str, candidates: list[dict]) -> dict:
    """Replay the fallback chain over the logged candidates.

    Mirrors run_fallback_chain: the first route's rejected candidate may be
    rescued at once; later routes are tried in order; when all are rejected
    the last-resort rescue picks among them. Provider latency, deadlines and
    provisional promotion are not replayed.
    """
    reasons: list[str] = []
    rejected: list[dict] = []
    for index, candidate in enumerate(candidates):
        verdict = _adjudicate_translation_candidate(
            SimpleNamespace(engine_name=candidate["engine"]), candidate["text"], source
        )
        reasons.append(verdict.reason or "")
        if not verdict.reason:
            return {
                "expect_candidate_reasons": reasons,
                "expect_outcome": "deepseek" if candidate["engine"] == "deepseek" else "groq",
                "expect_published": verdict.candidate_output,
            }
        rejected.append({"status": "rejected_output", "output_guard": verdict.evidence})
        if index == 0 and _choose_guard_rescue((rejected[0],), source):
            return {
                "expect_candidate_reasons": reasons,
                "expect_outcome": "rescue",
                "expect_published": verdict.candidate_output,
            }
    choice = _choose_guard_rescue(tuple(rejected), source)
    for candidate in candidates[len(reasons):]:
        reasons.append("not_reached")
    if choice:
        return {
            "expect_candidate_reasons": reasons,
            "expect_outcome": "rescue",
            "expect_published": rejected[choice["attempt_index"]]["output_guard"].get("candidate_output", ""),
        }
    return {"expect_candidate_reasons": reasons, "expect_outcome": "lost", "expect_published": ""}


def evaluate_case(
    source: str,
    shipped_target: str,
    profile_id: str,
    candidates: list[dict] | None = None,
) -> dict:
    """Run the deterministic layers for one case. Fresh policy per case so
    stateful checks (duplicate/last_input) cannot couple cases together."""
    with _active_profile(profile_id):
        policy = _new_translation_policy()
        rejection = policy.rejection_reason(source)
        norm = _normalize_source_before_matching(source)
        target = _apply_source_aware_corrections(source, shipped_target) if shipped_target else ""
        result = {
            "expect_rejection": rejection,
            "expect_norm_source": norm,
            "expect_target": target,
            "expect_capsule": _resolve_entity_request_context(source).capsule,
        }
        if candidates:
            result.update(_publication(source, candidates))
    return result


def event_candidates(event: dict) -> list[dict]:
    """Provider candidates of one translation event, in route order."""
    candidates = []
    for attempt in event.get("attempts") or []:
        guard = attempt.get("output_guard") if isinstance(attempt.get("output_guard"), dict) else {}
        text = guard.get("candidate_raw_output")
        if not text and attempt.get("status") == "success" and attempt.get("selected_for_output"):
            text = event.get("target_text")  # accepted raw output is not logged
        if text and attempt.get("engine"):
            candidates.append({"engine": attempt["engine"], "text": text})
    return candidates


def _had_rejection(event: dict) -> bool:
    return any(
        attempt.get("status") == "rejected_output" for attempt in event.get("attempts") or []
    )


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------

def iter_translation_events(patterns: list[str], run_kind: str = "default"):
    from utils.run_kind_filter import matches_run_kind
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
                    if not matches_run_kind(event, run_kind):
                        continue
                    if event.get("engine") == "mock":  # test pollution guard
                        continue
                    event["_day"] = day
                    yield event


def case_id(source: str, profile_id: str) -> str:
    return hashlib.sha256(f"{profile_id}\x00{source}".encode("utf-8")).hexdigest()[:16]


def build(args) -> int:
    events = list(iter_translation_events(args.events, args.run_kind))
    if not events:
        print("no translation events matched — check --events globs", file=sys.stderr)
        return 1

    # Stratify: spread cases across (profile, day), dedupe identical sources,
    # keep filtered events too (policy regressions are exactly about those).
    # Every sentence with a rejected candidate is kept outside the quota:
    # guard and name-data changes act on exactly those.
    buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    guard_events: list[dict] = []
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
        if _had_rejection(event):
            guard_events.append(event)
        else:
            buckets[(profile_id, event["_day"])].append(event)

    cases: list[dict] = []
    per_profile: Counter = Counter()

    def add_case(event: dict) -> None:
        source = (event.get("source_text") or "").strip()
        profile_id = event.get("profile_id") or ""
        shipped_target = event.get("target_text") or ""
        candidates = event_candidates(event)
        case = {
            "case_id": case_id(source, profile_id),
            "profile_id": profile_id,
            "day": event["_day"],
            "source": source,
            "shipped_status": event.get("status"),
            "shipped_filter_reason": event.get("filter_reason") or "",
            "shipped_target": shipped_target,
        }
        if candidates:
            case["candidates"] = candidates
        case.update(evaluate_case(source, shipped_target, profile_id, candidates))
        cases.append(case)

    for event in guard_events:
        add_case(event)
    # round-robin over (profile, day) buckets for even coverage
    bucket_lists = sorted(buckets.items())
    index = 0
    while bucket_lists:
        remaining = []
        for (profile_id, day), bucket in bucket_lists:
            if per_profile[profile_id] >= args.per_profile:
                continue
            if index < len(bucket):
                add_case(bucket[index])
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
    print(f"snapshot written: {out} ({len(cases)} cases, "
          f"{len(guard_events)} with a rejected candidate)")
    print("per profile:", dict(per_profile))
    print("guard outcomes:", dict(Counter(c.get("expect_outcome") for c in cases if "candidates" in c)))
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
    compared = (
        ("POLICY", "expect_rejection"),
        ("NORM", "expect_norm_source"),
        ("TARGET", "expect_target"),
        ("CAPSULE", "expect_capsule"),
        ("GUARD", "expect_candidate_reasons"),
        ("OUTCOME", "expect_outcome"),
        ("PUBLISHED", "expect_published"),
    )
    for case in cases:
        current = evaluate_case(
            case["source"], case["shipped_target"], case["profile_id"], case.get("candidates")
        )
        # Older snapshot rows predate a field; they are compared once updated.
        kinds = [
            kind for kind, key in compared
            if key in case and current.get(key) != case[key]
        ]
        if kinds:
            diffs.append(("+".join(kinds), case, current))
            if args.update:
                case.update(current)

    print(f"replayed {len(cases)} cases — {len(diffs)} diverge from snapshot")
    summary = Counter(kind for kind, _, _ in diffs)
    if summary:
        print("by kind:", dict(summary))
    transitions = Counter(
        f"{case['expect_outcome']} -> {current['expect_outcome']}"
        for kind, case, current in diffs if "OUTCOME" in kind
    )
    if transitions:
        print("subtitle outcome changes:", dict(transitions))
    lost = sum(
        1 for kind, case, current in diffs
        if "OUTCOME" in kind and current["expect_outcome"] == "lost"
    )
    if lost:
        print(f"WARNING: {lost} sentence(s) that had a subtitle would now have none")
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
        if "CAPSULE" in kind:
            print(f"  capsule: {case['expect_capsule'][-80:]!r} -> {current['expect_capsule'][-80:]!r}")
        if "GUARD" in kind:
            print(f"  candidates: {case['expect_candidate_reasons']} -> {current['expect_candidate_reasons']}")
        if "OUTCOME" in kind or "PUBLISHED" in kind:
            print(f"  outcome: {case['expect_outcome']} {case['expect_published'][:40]!r} -> "
                  f"{current['expect_outcome']} {current['expect_published'][:40]!r}")

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
    p_build.add_argument("--run-kind", choices=("default", "live", "test", "replay", "benchmark", "cafe_clip", "all"), default="default")
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
