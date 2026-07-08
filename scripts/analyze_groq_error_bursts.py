"""Catalogue Groq STT generic-error bursts (offline, read-only).

Item 4 of DETERMINISTIC_FIXES_PROPOSAL_20260624.md. Scans every runtime-events file,
groups STT events per run in time order, and finds maximal consecutive runs of
`status=failed, reason=error` events (generic errors, separate from rate_limited).

It reports the consecutive-error run-length distribution so the burst-detection
parameters N/T can be chosen from data rather than assumed, the lost-utterance count,
the error-latency distribution (to show how much of `reason=error` coincides with the
client timeout), and a fallback-logging finding (whether any engine-switch is even
recorded). It does not read or modify live modules.
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _stt_events_by_run(paths: list[str]) -> dict[str, list[dict]]:
    runs: dict[str, list[dict]] = {}
    for path in paths:
        with open(path, encoding="utf-8-sig", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(e, dict) or e.get("event_type") != "stt":
                    continue
                runs.setdefault(e.get("run_id") or "", []).append(e)
    for events in runs.values():
        events.sort(key=lambda e: e.get("created_at") or "")
    return runs


def _is_generic_error(e: dict) -> bool:
    return e.get("status") == "failed" and e.get("reason") == "error"


def find_error_runs(events: list[dict]) -> list[list[dict]]:
    """Maximal consecutive runs of generic-error STT events (time-ordered)."""
    runs: list[list[dict]] = []
    current: list[dict] = []
    for e in events:
        if _is_generic_error(e):
            current.append(e)
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    return runs


def classify_error_utterance_outcomes(events: list[dict]) -> list[dict]:
    """Classify generic-error attempts by linked utterance outcome.

    Current logs reuse one utterance_id across the primary attempt, the cross-key
    retry, and the eventual success. Older rows without an utterance_id remain
    unknown rather than being silently counted as lost.
    """
    ordered_ids: list[str] = []
    by_utterance: dict[str, list[dict]] = {}
    unknown_index = 0
    for event in events:
        utterance_id = str(event.get("utterance_id") or "")
        if not utterance_id:
            if not _is_generic_error(event):
                continue
            unknown_index += 1
            utterance_id = f"__unknown__{unknown_index}"
        if utterance_id not in by_utterance:
            ordered_ids.append(utterance_id)
            by_utterance[utterance_id] = []
        by_utterance[utterance_id].append(event)

    outcomes: list[dict] = []
    for utterance_id in ordered_ids:
        linked = by_utterance[utterance_id]
        generic_errors = [event for event in linked if _is_generic_error(event)]
        if not generic_errors:
            outcomes.append({"utterance_id": utterance_id, "outcome": "non_error"})
            continue
        if utterance_id.startswith("__unknown__"):
            outcome = "unknown"
        elif any(event.get("status") == "success" for event in linked):
            outcome = "rescued"
        else:
            outcome = "lost"
        outcomes.append({
            "utterance_id": "" if utterance_id.startswith("__unknown__") else utterance_id,
            "outcome": outcome,
            "generic_error_attempts": len(generic_errors),
            "start": generic_errors[0].get("created_at"),
            "end": generic_errors[-1].get("created_at"),
        })
    return outcomes


def lost_utterance_run_lengths(outcomes: list[dict]) -> list[int]:
    lengths: list[int] = []
    current = 0
    for outcome in outcomes:
        if outcome.get("outcome") == "lost":
            current += 1
            continue
        if current:
            lengths.append(current)
            current = 0
    if current:
        lengths.append(current)
    return lengths


def _run_span_seconds(run: list[dict]) -> float | None:
    start = _parse_ts(run[0].get("created_at") or "")
    end = _parse_ts(run[-1].get("created_at") or "")
    if start and end:
        return round((end - start).total_seconds(), 1)
    return None


def build_report(paths: list[str]) -> dict:
    runs = _stt_events_by_run(paths)

    total_stt = sum(len(v) for v in runs.values())
    total_errors = 0
    run_length_hist: Counter[int] = Counter()
    bursts: list[dict] = []
    engines_seen: Counter[str] = Counter()
    error_latencies: list[float] = []
    errors_by_day: Counter[str] = Counter()
    outcome_counts: Counter[str] = Counter()
    lost_run_hist: Counter[int] = Counter()
    error_attempts_with_utterance_id = 0
    attempt_index_present = 0
    key_role_present = 0
    will_retry_present = 0

    for run_id, events in runs.items():
        for e in events:
            engines_seen[e.get("engine") or ""] += 1
            if _is_generic_error(e) and e.get("utterance_id"):
                error_attempts_with_utterance_id += 1
            if "attempt_index" in e:
                attempt_index_present += 1
            if "key_role" in e:
                key_role_present += 1
            if "will_retry" in e:
                will_retry_present += 1
        outcomes = classify_error_utterance_outcomes(events)
        for outcome in outcomes:
            if outcome.get("outcome") in {"rescued", "lost", "unknown"}:
                outcome_counts[outcome["outcome"]] += 1
        for length in lost_utterance_run_lengths(outcomes):
            lost_run_hist[length] += 1
        error_runs = find_error_runs(events)
        for er in error_runs:
            run_length_hist[len(er)] += 1
            total_errors += len(er)
            for e in er:
                day = str(e.get("created_at") or "")[:10]
                errors_by_day[day] += 1
                lat = e.get("latency_ms")
                if isinstance(lat, (int, float)):
                    error_latencies.append(float(lat))
            if len(er) >= 3:  # noteworthy multi-error bursts
                utterance_ids = [str(e.get("utterance_id") or "") for e in er]
                linked_ids = sorted({uid for uid in utterance_ids if uid})
                linked_outcomes = {
                    outcome["utterance_id"]: outcome["outcome"]
                    for outcome in outcomes
                    if outcome.get("utterance_id") in linked_ids
                }
                bursts.append({
                    "run_id": run_id,
                    "length": len(er),
                    "start": er[0].get("created_at"),
                    "end": er[-1].get("created_at"),
                    "span_seconds": _run_span_seconds(er),
                    "profile_id": er[0].get("profile_id"),
                    "engines": sorted({e.get("engine") for e in er}),
                    "distinct_linked_utterances": len(linked_ids),
                    "linked_utterance_outcomes": linked_outcomes,
                    "unlinked_error_attempts": sum(1 for uid in utterance_ids if not uid),
                })

    bursts.sort(key=lambda b: (-b["length"], b["start"] or ""))
    error_latencies.sort()

    def _pct(p: float) -> float | None:
        if not error_latencies:
            return None
        return error_latencies[min(len(error_latencies) - 1, int(len(error_latencies) * p))]

    # Intra-run gaps among the longest bursts, to inform the T (time-window) parameter.
    longest = max((er for events in runs.values() for er in find_error_runs(events)),
                  key=len, default=[])
    longest_gaps = []
    for a, b in zip(longest, longest[1:]):
        ta, tb = _parse_ts(a.get("created_at") or ""), _parse_ts(b.get("created_at") or "")
        if ta and tb:
            longest_gaps.append(round((tb - ta).total_seconds(), 1))

    return {
        "groq_error_burst_schema": 2,
        "regenerate_command": (
            "live-subtitle-env\\Scripts\\python.exe scripts\\analyze_groq_error_bursts.py "
            "--events \"logs/runtime_events_202605*.jsonl\" "
            "\"logs/runtime_events_2026060*.jsonl\" "
            "\"logs/runtime_events_2026061*.jsonl\" "
            "\"logs/runtime_events_2026062[0-4].jsonl\""
        ),
        "totals": {
            "stt_events": total_stt,
            "generic_error_events": total_errors,
            "runs_scanned": len(runs),
        },
        "consecutive_error_run_length_distribution": dict(sorted(run_length_hist.items())),
        "attempt_vs_utterance": {
            "generic_error_attempts": total_errors,
            "attempts_with_utterance_id": error_attempts_with_utterance_id,
            "linked_utterance_outcomes": dict(outcome_counts),
            "consecutive_lost_utterance_run_length_distribution": dict(
                sorted(lost_run_hist.items())
            ),
            "note": "The attempt-run histogram is not a lost-subtitle histogram. One "
                    "utterance can emit two failed attempts before a retry succeeds or "
                    "the utterance is finally lost.",
        },
        "multi_error_bursts_ge3": bursts,
        "errors_by_day": dict(sorted(errors_by_day.items())),
        "error_latency_ms": {
            "count": len(error_latencies),
            "p50": _pct(0.50),
            "p90": _pct(0.90),
            "max": error_latencies[-1] if error_latencies else None,
            "note": "If p50 clusters near config.stt.groq_timeout (10000 ms), much of "
                    "reason=error is the client timeout rather than a server error.",
        },
        "longest_burst_intra_gaps_seconds": longest_gaps,
        "current_behavior_code": {
            "source": "working tree modules/stt.py exception branch; compare git diff",
            "generic_error_retry": "A non-rate-limit exception (reason=error) is terminal for "
                                   "the chunk and does not select the other Groq key. Immediate "
                                   "cross-key retry remains limited to rate-limit failures.",
            "working_tree_provenance": "The working-tree diagnostic fields do not add a "
                                       "generic-error retry. Historical rows must still be "
                                       "interpreted from their recorded utterance linkage rather "
                                       "than projected current behavior.",
            "sensevoice_failover": "With primary_engine=groq, __init__ initializes Groq clients "
                                   "and does not load SenseVoice. The periodic probe only runs "
                                   "when a SenseVoice model was loaded earlier after local-primary "
                                   "failure. Default Groq mode therefore has no already-loaded "
                                   "SenseVoice burst fallback.",
            "groq_max_retries": "config.stt.groq_max_retries=0, so the Groq SDK itself does not "
                                "retry; the application only performs its existing one-shot "
                                "cross-key retry for rate-limit failures.",
        },
        "engine_field_finding": {
            "stt_engines_seen": dict(engines_seen),
            "engine_switch_field_present": False,
            "utterance_linkage_present_on_error_attempts": error_attempts_with_utterance_id,
            "attempt_index_present": attempt_index_present,
            "key_role_present": key_role_present,
            "will_retry_present": will_retry_present,
            "note": "Historical STT events in this corpus lack explicit "
                    "key-role/attempt-index/will-retry fields, but newer historical rows "
                    "can link attempts and success by utterance_id. "
                    "The working tree now emits all three explicit fields; this corpus "
                    "predates that implementation.",
        },
        "error_outcome_determinability": {
            "generic_error_events": total_errors,
            "rescued_vs_lost": "partially_determinable_from_utterance_linkage",
            "linked_rescued_utterances": outcome_counts["rescued"],
            "linked_lost_utterances": outcome_counts["lost"],
            "unlinked_unknown_attempts": outcome_counts["unknown"],
            "reason": "Rows with utterance_id can be joined to a same-id success or terminal "
                      "failure. Older rows without linkage remain unknown.",
            "burst_error_events_ge3": sum(b["length"] for b in bursts),
        },
        "policy_assessment": {
            "raw_attempt_threshold_n3_status": "rejected: N=3 attempts can occur during only "
                                               "two utterances",
            "lost_utterance_signal": "STTEngine._consecutive_none already increments once only "
                                     "after all Groq attempts for a chunk return None",
            "sensevoice_burst_failover_status": "blocked: default Groq mode has no preloaded "
                                                "SenseVoice model; lazy loading would block the "
                                                "live STT thread and adds unmeasured GPU cost",
            "diagnostic_change_status": "implemented in working tree without behavior changes",
            "safe_next_change": "collect a version-identifiable run containing "
                                "attempt_index/key_role/will_retry before selecting a live "
                                "failover policy",
        },
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Catalogue Groq STT generic-error bursts.")
    parser.add_argument("--events", nargs="+", required=True)
    parser.add_argument("--output", type=Path,
                        default=PROJECT_ROOT / "scratch" / "analysis" / "groq_error_burst_20260624.json")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    paths: list[str] = []
    for pattern in args.events:
        paths.extend(sorted(glob.glob(pattern)))
    if not paths:
        print("no event files matched", file=sys.stderr)
        return 1
    report = build_report(paths)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    t = report["totals"]
    print(f"Wrote {args.output} | errors={t['generic_error_events']} "
          f"bursts>=3={len(report['multi_error_bursts_ge3'])} "
          f"runlen={report['consecutive_error_run_length_distribution']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
