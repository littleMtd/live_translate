"""Attribute the translation latency tail to components and engines (offline, read-only).

Item 2 of DETERMINISTIC_FIXES_PROPOSAL_20260624.md. Complements
scripts/analyze_runtime_events.py (broad summary) with a focused tail attribution:
for the top latency percentile it splits engine-call time vs queue vs predecessor
stall, breaks the tail down per engine, and compares each engine's observed
engine_latency against its configured timeout so an unenforced timeout is visible.

config.py baseline values are passed in (not imported) so the report is explicit
about the assumed knobs; reading config is allowed, mutating it is not.
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

def _load(paths: list[str]) -> list[dict]:
    out = []
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
                if (isinstance(e, dict) and e.get("event_type") == "translation"
                        and isinstance(e.get("schema_version"), int)
                        and e["schema_version"] >= 2 and e.get("status") == "success"):
                    out.append(e)
    return out


def _pcts(values: list[float]) -> dict:
    if not values:
        return {}
    s = sorted(values)
    def p(q): return s[min(len(s) - 1, int(len(s) * q))]
    return {"p50": p(.5), "p90": p(.9), "p95": p(.95), "p99": p(.99), "max": s[-1], "n": len(s)}


def _num(e: dict, field: str) -> float:
    v = e.get(field)
    return float(v) if isinstance(v, (int, float)) else 0.0


def _attempts(event: dict) -> list[dict]:
    raw = event.get("attempts")
    return [row for row in raw if isinstance(row, dict)] if isinstance(raw, list) else []


def _attempt_timeout_ms(attempt: dict) -> float:
    return _num(attempt, "api_attempt_timeout_ms") or _num(attempt, "timeout_config_ms")


def _comparable_attempt_wall_ms(attempt: dict) -> float:
    final_attempt = _num(attempt, "api_final_attempt_ms")
    if final_attempt > 0:
        return final_attempt
    if _num(attempt, "api_attempt_count") <= 1:
        return _num(attempt, "api_total_wall_ms")
    return 0.0


def _tail_quantile_arg(value: str) -> float:
    try:
        quantile = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("tail quantile must be a number") from exc
    if not 0.0 <= quantile < 1.0:
        raise argparse.ArgumentTypeError("tail quantile must be >= 0 and < 1")
    return quantile


def build_report(paths: list[str], *, tail_quantile: float = 0.95) -> dict:
    if not 0.0 <= tail_quantile < 1.0:
        raise ValueError("tail_quantile must be >= 0 and < 1")
    ev = _load(paths)
    lat_all = [_num(e, "latency_ms") for e in ev]
    overall = _pcts(lat_all)
    s = sorted(lat_all)
    thr = s[int(len(s) * tail_quantile)] if s else 0
    tail = [e for e in ev if _num(e, "latency_ms") >= thr]

    # `engine_latency_ms` is emitted from translator.translate_item's elapsed timer,
    # which wraps the entire Translator.translate_event call.  It can include earlier
    # fallback engines and must not be interpreted as a single final-engine API call.
    worker_elapsed_matches = sum(
        1
        for e in tail
        if _num(e, "engine_latency_ms")
        and abs(_num(e, "latency_ms") - _num(e, "engine_latency_ms"))
        <= 0.1 * max(1.0, _num(e, "latency_ms"))
    )
    stall_dom = sum(1 for e in tail if _num(e, "predecessor_stall_ms") > 0.5 * max(1.0, _num(e, "latency_ms")))
    queue_dom = sum(1 for e in tail if _num(e, "queue_wait_ms") > 0.5 * max(1.0, _num(e, "latency_ms")))
    timeout_tail = sum(1 for e in tail if _num(e, "api_timeout_count") > 0)
    api_wall_rows = [e for e in tail if _num(e, "api_total_wall_ms") > 0]
    final_api_dom = sum(
        1
        for e in api_wall_rows
        if _num(e, "api_total_wall_ms") >= 0.8 * max(1.0, _num(e, "latency_ms"))
    )
    prior_path_dom = sum(
        1
        for e in api_wall_rows
        if _num(e, "latency_ms") - _num(e, "api_total_wall_ms")
        >= 0.5 * max(1.0, _num(e, "latency_ms"))
    )
    tail_attempts = [attempt for event in tail for attempt in _attempts(event)]

    # Per-engine tail breakdown + observed engine_latency vs configured timeout.
    per_engine = {}
    engines = {
        str(engine)
        for engine in (
            *(e.get("engine") for e in tail),
            *(attempt.get("engine") for attempt in tail_attempts),
        )
        if engine
    }
    for engine in sorted(engines):
        rows = [e for e in tail if e.get("engine") == engine]
        eng_lat = _pcts([_num(e, "engine_latency_ms") for e in rows])
        final_api_lat = _pcts([
            _num(e, "api_total_wall_ms")
            for e in rows
            if _num(e, "api_total_wall_ms") > 0
        ])
        residual = _pcts([
            max(0.0, _num(e, "latency_ms") - _num(e, "api_total_wall_ms"))
            for e in rows
            if _num(e, "api_total_wall_ms") > 0
        ])
        engine_attempts = [
            attempt for attempt in tail_attempts if attempt.get("engine") == engine
        ]
        timeout_values = sorted({
            _attempt_timeout_ms(attempt)
            for attempt in engine_attempts
            if _attempt_timeout_ms(attempt) > 0
        })
        timed_attempts = [
            attempt for attempt in engine_attempts
            if _attempt_timeout_ms(attempt) > 0
            and _comparable_attempt_wall_ms(attempt) > 0
        ]
        over_timeout = sum(
            1 for attempt in timed_attempts
            if _comparable_attempt_wall_ms(attempt) > _attempt_timeout_ms(attempt) * 1.1
        )
        per_engine[engine] = {
            "tail_count": len(rows),
            "engine_latency_ms": eng_lat,
            "final_api_total_wall_ms": final_api_lat,
            "pre_final_api_residual_ms": residual,
            "final_api_diagnostics_coverage": (
                final_api_lat.get("n", 0) / len(rows) if rows else 0.0
            ),
            "attempt_ledger_rows": len(engine_attempts),
            "comparable_attempt_timeout_coverage": (
                len(timed_attempts) / len(engine_attempts) if engine_attempts else 0.0
            ),
            "recorded_timeout_ms": timeout_values,
            "attempts_over_recorded_timeout_margin": over_timeout,
            "timeout_appears_unenforced": bool(timed_attempts and over_timeout),
        }

    top = sorted(tail, key=lambda e: -_num(e, "latency_ms"))[:10]
    top_rows = [{
        "latency_ms": int(_num(e, "latency_ms")),
        "engine": e.get("engine"),
        "engine_latency_ms": int(_num(e, "engine_latency_ms")),
        "api_attempt_count": e.get("api_attempt_count"),
        "api_timeout_count": e.get("api_timeout_count"),
        "api_total_wall_ms": int(_num(e, "api_total_wall_ms")),
        "pre_final_api_residual_ms": int(max(
            0.0, _num(e, "latency_ms") - _num(e, "api_total_wall_ms")
        )),
        "queue_wait_ms": int(_num(e, "queue_wait_ms")),
        "predecessor_stall_ms": int(_num(e, "predecessor_stall_ms")),
        "forced": e.get("forced"),
        "schema_version": e.get("schema_version"),
        "attempt_count": len(_attempts(e)),
    } for e in top]

    return {
        "latency_tail_schema": 2,
        "regenerate_command": (
            "live-subtitle-env\\Scripts\\python.exe scripts\\analyze_latency_tail.py "
            "--events \"logs/runtime_events_2026061*.jsonl\" "
            "\"logs/runtime_events_2026062[0-4].jsonl\""
        ),
        "population": "schema_version>=2 success translations",
        "schema_version_distribution": dict(Counter(e.get("schema_version") for e in ev)),
        "overall_latency_ms": overall,
        "tail_quantile": tail_quantile,
        "tail_threshold_ms": thr,
        "tail_count": len(tail),
        "tail_domination": {
            "worker_elapsed_field_~=_translation_latency": worker_elapsed_matches,
            "predecessor_stall_gt_50pct": stall_dom,
            "queue_wait_gt_50pct": queue_dom,
            "had_api_timeout": timeout_tail,
            "final_api_diagnostics_rows": len(api_wall_rows),
            "final_api_wall_gt_80pct": final_api_dom,
            "pre_final_api_residual_gt_50pct": prior_path_dom,
            "interpretation": "engine_latency_ms is worker elapsed around the whole "
                              "translate_event call and cannot distinguish a single engine "
                              "from fallback serialization. api_total_wall_ms covers only the "
                              "final engine diagnostics; compare it with latency_ms where present.",
        },
        "tail_engine_distribution": dict(Counter(e.get("engine") for e in tail)),
        "per_engine": per_engine,
        "top10_by_latency": top_rows,
        "timeout_evidence": {
            "source": "per-attempt runtime ledger",
            "margin": "comparable single/final attempt wall time > 1.1 * recorded attempt timeout",
            "limitation": "Rows without attempt ledgers, recorded timeout values, or a single/final-attempt wall time remain unattributable.",
        },
        "mode_coverage": {
            "translation_mode_present": sum(
                1 for e in ev if isinstance(e.get("translation_mode"), str)
            ),
            "translation_events": len(ev),
            "finding": "NVIDIA live-vs-clip timeout attribution is unavailable when mode is absent.",
            "working_tree_status": "translation events now emit translation_mode; this "
                                   "historical corpus predates the diagnostic and remains "
                                   "unattributable until a new run is collected",
        },
        "caveats": {
            "timeout_appears_unenforced_is_heuristic": "The 10% margin flags a runtime wall-time "
                "gap for code verification; it does not prove which blocking operation exceeded its deadline.",
            "legacy_rows": "Schema-v2 rows without attempt ledgers remain usable for aggregate "
                "latency, but cannot support attempt-level timeout attribution.",
        },
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Attribute the translation latency tail.")
    parser.add_argument("--events", nargs="+", required=True)
    parser.add_argument("--tail-quantile", type=_tail_quantile_arg, default=0.95)
    parser.add_argument("--output", type=Path,
                        default=PROJECT_ROOT / "scratch" / "analysis" / "latency_tail_20260624.json")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    paths: list[str] = []
    for pattern in args.events:
        paths.extend(sorted(glob.glob(pattern)))
    if not paths:
        print("no event files matched", file=sys.stderr)
        return 1
    report = build_report(paths, tail_quantile=args.tail_quantile)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    unenforced = [eng for eng, v in report["per_engine"].items() if v["timeout_appears_unenforced"]]
    print(f"Wrote {args.output} | tail_n={report['tail_count']} "
          f"engines={report['tail_engine_distribution']} timeout_unenforced={unenforced}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
