"""Compare fixed-Korean and auto-detect Groq STT on the same historical WAVs.

This is deliberately a no-label, record-only experiment. It can detect clear
regression proxies (empty output, Korean misclassified as another language,
new kana/Latin hallucinations), but engine disagreement is never called a
correction or a quality win without ground truth.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path
from statistics import median
from typing import Any, Callable, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import cfg
from modules.streamer_profiles import build_stt_glossary
from modules.stt_policy import build_groq_prompt_budget, segment_stats

DEFAULT_LOG_DIR = PROJECT_ROOT / "logs"
DEFAULT_AUDIO_ROOT = DEFAULT_LOG_DIR / "audio_dump"
DEFAULT_OUTPUT = PROJECT_ROOT / "scratch" / "analysis" / "stt_language_mode_comparison_20260713.json"
_STRATA = ("latin_heavy", "kana_present", "low_confidence", "quality_risk", "baseline")


def iter_events(paths: Iterable[Path]) -> Iterable[dict[str, Any]]:
    for path in paths:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    yield event


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _script_stats(text: str) -> dict[str, float | int]:
    compact = [char for char in str(text or "") if not char.isspace()]
    total = max(1, len(compact))
    hangul = sum("\uac00" <= char <= "\ud7a3" for char in compact)
    kana = sum("\u3040" <= char <= "\u30ff" for char in compact)
    latin = sum(char.isascii() and char.isalpha() for char in compact)
    return {
        "char_count": len(compact),
        "hangul_ratio": round(hangul / total, 3),
        "kana_count": kana,
        "latin_ratio": round(latin / total, 3),
    }


def _candidate_strata(event: dict[str, Any]) -> list[str]:
    source = str(event.get("source_text") or "")
    stats = _script_stats(source)
    strata = []
    if float(stats["latin_ratio"]) >= 0.35:
        strata.append("latin_heavy")
    if int(stats["kana_count"]) > 0:
        strata.append("kana_present")
    avg_logprob = event.get("avg_logprob")
    if isinstance(avg_logprob, (int, float)) and avg_logprob <= -0.55:
        strata.append("low_confidence")
    if str(event.get("quality_severity") or "ok") != "ok" or event.get("quality_flags"):
        strata.append("quality_risk")
    return strata or ["baseline"]


def _stable_order(row: dict[str, Any]) -> str:
    key = f"{row['run_id']}:{row['utterance_id']}".encode("utf-8")
    return hashlib.sha256(key).hexdigest()


def select_candidates(
    events: Iterable[dict[str, Any]],
    *,
    audio_root: Path,
    limit: int,
) -> list[dict[str, Any]]:
    """Select deterministic one-utterance cases without creating annotations."""
    population = []
    seen: set[tuple[str, str]] = set()
    for event in events:
        utterance_ids = event.get("source_utterance_ids")
        if (
            event.get("event_type") != "translation"
            or event.get("status") != "success"
            or event.get("incomplete")
            or not isinstance(utterance_ids, list)
            or len(utterance_ids) != 1
            or int(event.get("source_count") or 1) != 1
            or not str(event.get("source_text") or "").strip()
        ):
            continue
        run_id = str(event.get("run_id") or "")
        utterance_id = str(utterance_ids[0] or "")
        key = run_id, utterance_id
        audio_path = audio_root / run_id / f"{utterance_id}.wav"
        if not run_id or not utterance_id or key in seen or not audio_path.is_file():
            continue
        seen.add(key)
        population.append(
            {
                "run_id": run_id,
                "utterance_id": utterance_id,
                "created_at": event.get("created_at"),
                "audio_path": _display_path(audio_path),
                "historical_source_text": str(event.get("source_text") or ""),
                "historical_avg_logprob": event.get("avg_logprob"),
                "historical_no_speech_prob": event.get("no_speech_prob"),
                "quality_severity": event.get("quality_severity"),
                "quality_flags": event.get("quality_flags") or [],
                "strata": _candidate_strata(event),
            }
        )

    population.sort(key=_stable_order)
    selected = []
    selected_keys: set[tuple[str, str]] = set()
    per_stratum = max(1, max(0, limit) // len(_STRATA))
    for stratum in _STRATA:
        for row in population:
            key = row["run_id"], row["utterance_id"]
            if key in selected_keys or stratum not in row["strata"]:
                continue
            selected.append(row)
            selected_keys.add(key)
            if sum(stratum in item["strata"] for item in selected) >= per_stratum:
                break
    for row in population:
        if len(selected) >= max(0, limit):
            break
        key = row["run_id"], row["utterance_id"]
        if key not in selected_keys:
            selected.append(row)
            selected_keys.add(key)
    return selected[: max(0, limit)]


def _normalized(text: str) -> str:
    return re.sub(r"\W+", "", str(text or ""), flags=re.UNICODE).lower()


def analyze_pair(
    fixed: dict[str, Any],
    auto: dict[str, Any],
    historical_text: str = "",
) -> dict[str, Any]:
    fixed_text = str(fixed.get("text") or "")
    auto_text = str(auto.get("text") or "")
    fixed_stats = _script_stats(fixed_text)
    auto_stats = _script_stats(auto_text)
    historical_stats = _script_stats(historical_text or fixed_text)
    if fixed.get("error") or auto.get("error"):
        return {
            "similarity": None,
            "changed": None,
            "comparable": False,
            "fixed_stats": fixed_stats,
            "auto_stats": auto_stats,
            "historical_stats": historical_stats,
            "regression_proxy_flags": [],
            "observation_signals": [],
        }
    flags = []
    signals = []
    if fixed_text and not auto_text:
        flags.append("auto_empty")
    auto_language = str(auto.get("language") or "").lower()
    historical_hangul = float(historical_stats["hangul_ratio"]) >= 0.5
    historical_kana = int(historical_stats["kana_count"]) > 0
    if historical_hangul and auto_language not in ("ko", "korean"):
        flags.append("auto_non_ko_on_hangul_baseline")
    if historical_hangul and float(fixed_stats["hangul_ratio"]) - float(auto_stats["hangul_ratio"]) >= 0.3:
        flags.append("hangul_ratio_drop_ge_0_3")
    if historical_kana and auto_language in ("ja", "japanese"):
        signals.append("auto_japanese_on_historical_kana")
    elif int(fixed_stats["kana_count"]) == 0 and int(auto_stats["kana_count"]) > 0:
        flags.append("introduced_kana")
    if float(historical_stats["latin_ratio"]) < 0.2 and float(auto_stats["latin_ratio"]) >= 0.5:
        flags.append("introduced_latin_heavy")
    similarity = difflib.SequenceMatcher(None, _normalized(fixed_text), _normalized(auto_text)).ratio()
    return {
        "similarity": round(similarity, 3),
        "changed": _normalized(fixed_text) != _normalized(auto_text),
        "comparable": True,
        "fixed_stats": fixed_stats,
        "auto_stats": auto_stats,
        "historical_stats": historical_stats,
        "regression_proxy_flags": flags,
        "observation_signals": signals,
    }


def run_pairs(
    candidates: list[dict[str, Any]],
    *,
    transcribe: Callable[[Path, str], dict[str, Any]],
    project_root: Path = PROJECT_ROOT,
) -> list[dict[str, Any]]:
    results = []
    for candidate in candidates:
        audio_path = Path(candidate["audio_path"])
        if not audio_path.is_absolute():
            audio_path = project_root / audio_path
        fixed = transcribe(audio_path, "fixed_ko")
        auto = transcribe(audio_path, "auto_detect")
        results.append(
            {
                **candidate,
                "fixed_ko": fixed,
                "auto_detect": auto,
                "comparison": analyze_pair(
                    fixed, auto, str(candidate.get("historical_source_text") or "")
                ),
            }
        )
    return results


def resume_pairs(
    candidates: list[dict[str, Any]],
    previous_results: list[dict[str, Any]],
    *,
    transcribe: Callable[[Path, str], dict[str, Any]],
    project_root: Path = PROJECT_ROOT,
) -> list[dict[str, Any]]:
    """Retry only failed/missing sides of an interrupted or rate-limited run."""
    previous = {
        (str(row.get("run_id") or ""), str(row.get("utterance_id") or "")): row
        for row in previous_results
    }
    results = []
    for candidate in candidates:
        key = str(candidate["run_id"]), str(candidate["utterance_id"])
        old = previous.get(key, {})
        audio_path = Path(candidate["audio_path"])
        if not audio_path.is_absolute():
            audio_path = project_root / audio_path
        modes = {}
        for mode in ("fixed_ko", "auto_detect"):
            prior = old.get(mode)
            modes[mode] = (
                prior
                if isinstance(prior, dict) and not prior.get("error")
                else transcribe(audio_path, mode)
            )
        results.append(
            {
                **candidate,
                **modes,
                "comparison": analyze_pair(
                    modes["fixed_ko"],
                    modes["auto_detect"],
                    str(candidate.get("historical_source_text") or ""),
                ),
            }
        )
    return results


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def build_report(candidates: list[dict[str, Any]], results: list[dict[str, Any]]) -> dict[str, Any]:
    proxy_counts: Counter[str] = Counter()
    signal_counts: Counter[str] = Counter()
    language_counts = {"fixed_ko": Counter(), "auto_detect": Counter()}
    latencies = {"fixed_ko": [], "auto_detect": []}
    error_counts: Counter[str] = Counter()
    comparable_count = 0
    for row in results:
        if row["comparison"].get("comparable", True):
            comparable_count += 1
            proxy_counts.update(row["comparison"]["regression_proxy_flags"])
            signal_counts.update(row["comparison"].get("observation_signals", []))
        for mode in ("fixed_ko", "auto_detect"):
            result = row[mode]
            if result.get("error"):
                error_counts[f"{mode}:{result['error']}"] += 1
                continue
            language_counts[mode][str(result.get("language") or "unknown")] += 1
            if isinstance(result.get("latency_ms"), (int, float)):
                latencies[mode].append(float(result["latency_ms"]))

    if not results:
        gate = "not_executed"
    elif error_counts:
        gate = "inconclusive_api_errors"
    elif proxy_counts:
        gate = "no_go"
    else:
        gate = "eligible_for_record_only_shadow"
    return {
        "schema": 1,
        "method": "paired_historical_wav_fixed_ko_vs_auto_detect_no_labels",
        "candidate_count": len(candidates),
        "executed_count": len(results),
        "comparable_pair_count": comparable_count,
        "ground_truth_count": 0,
        "correctness_claim": None,
        "gate": gate,
        "gate_rule": (
            "Any regression proxy or API error blocks live enablement. Passing only permits "
            "a separate record-only shadow; it never proves auto-detect is more accurate."
        ),
        "regression_proxy_counts": dict(sorted(proxy_counts.items())),
        "observation_signal_counts": dict(sorted(signal_counts.items())),
        "api_error_counts": dict(sorted(error_counts.items())),
        "language_counts": {
            mode: dict(sorted(counts.items())) for mode, counts in language_counts.items()
        },
        "latency_ms": {
            mode: {
                "mean": _mean(values),
                "median": round(median(values), 2) if values else None,
            }
            for mode, values in latencies.items()
        },
        "candidate_strata": dict(
            sorted(Counter(tag for row in candidates for tag in row["strata"]).items())
        ),
        "candidates": candidates,
        "results": results,
    }


def _current_prompt() -> str | None:
    budget = build_groq_prompt_budget(
        seed_prompt=cfg.stt.groq_prompt,
        use_profile_glossary=cfg.stt.use_profile_glossary,
        active_profile=cfg.active_streamer_profile,
        last_transcript="",
        glossary_builder=build_stt_glossary,
        max_context_chars=120,
        max_prompt_chars=896,
    )
    return budget.prompt


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--logs", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--audio-root", type=Path, default=DEFAULT_AUDIO_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--limit", type=int, default=24)
    parser.add_argument("--execute", action="store_true", help="Call Groq twice per selected WAV.")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Reuse successful sides from --output and retry only failures.",
    )
    parser.add_argument("--model", default=cfg.stt.groq_model)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument(
        "--key-role",
        choices=("primary", "fallback"),
        default="primary",
        help="Groq key used for this isolated replay run.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    previous_results: list[dict[str, Any]] = []
    if args.resume and args.output.is_file():
        previous_report = json.loads(args.output.read_text(encoding="utf-8"))
        candidates = list(previous_report.get("candidates") or [])
        previous_results = list(previous_report.get("results") or [])
    else:
        candidates = select_candidates(
            iter_events(sorted(args.logs.glob("runtime_events_*.jsonl"))),
            audio_root=args.audio_root,
            limit=args.limit,
        )
    results: list[dict[str, Any]] = []
    if args.execute and candidates:
        api_key = cfg.keys.groq_fallback if args.key_role == "fallback" else cfg.keys.groq
        if not api_key:
            print(f"Groq {args.key_role} key not set", file=sys.stderr)
            return 2
        from groq import Groq

        client = Groq(api_key=api_key, max_retries=0, timeout=args.timeout)
        prompt = _current_prompt()

        def transcribe(audio_path: Path, mode: str) -> dict[str, Any]:
            started = time.monotonic()
            try:
                with audio_path.open("rb") as audio_file:
                    request: dict[str, Any] = {
                        "model": args.model,
                        "file": audio_file,
                        "prompt": prompt,
                        "response_format": "verbose_json",
                        "temperature": 0.0,
                    }
                    if mode == "fixed_ko":
                        request["language"] = "ko"
                    response = client.audio.transcriptions.create(**request)
                segments = getattr(response, "segments", None) or []
                stats = segment_stats(segments)
                return {
                    "text": str(getattr(response, "text", "") or "").strip(),
                    "language": str(getattr(response, "language", "") or ""),
                    "latency_ms": round((time.monotonic() - started) * 1000, 1),
                    "avg_logprob": stats.logprob if stats else None,
                    "no_speech_prob": stats.no_speech if stats else None,
                    "compression_ratio": stats.compression_ratio if stats else None,
                    "error": None,
                }
            except Exception as exc:
                return {
                    "text": "",
                    "language": "",
                    "latency_ms": round((time.monotonic() - started) * 1000, 1),
                    "error": type(exc).__name__,
                }

        results = (
            resume_pairs(candidates, previous_results, transcribe=transcribe)
            if args.resume
            else run_pairs(candidates, transcribe=transcribe)
        )

    report = build_report(candidates, results)
    report.update(
        {
            "model": args.model,
            "profile": cfg.active_streamer_profile,
            "prompt_bytes": len((_current_prompt() or "").encode("utf-8")),
            "key_role": args.key_role if args.execute else "not_used",
        }
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "candidate_count": report["candidate_count"],
                "executed_count": report["executed_count"],
                "gate": report["gate"],
                "regression_proxy_counts": report["regression_proxy_counts"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
