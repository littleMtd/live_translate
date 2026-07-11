from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np
import soundfile as sf

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG_DIR = PROJECT_ROOT / "logs"
DEFAULT_AUDIO_ROOT = DEFAULT_LOG_DIR / "audio_dump"
DEFAULT_OUTPUT = PROJECT_ROOT / "scratch" / "analysis" / "sensevoice_historical_scout_20260711.json"
TAG_RE = re.compile(r"<\|[^>]*\|>")


def _events(paths: Iterable[Path]) -> Iterable[dict[str, Any]]:
    for path in paths:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    yield event


def _risk(event: dict[str, Any]) -> tuple[int, float, str]:
    severity = str(event.get("quality_severity") or "ok")
    rank = {"bad": 3, "warn": 2, "ok": 1}.get(severity, 0)
    avg_logprob = event.get("avg_logprob")
    confidence_risk = -float(avg_logprob) if isinstance(avg_logprob, (int, float)) else 0.0
    return rank + int(bool(event.get("forced"))), confidence_risk, str(event.get("created_at") or "")


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def select_candidates(
    events: Iterable[dict[str, Any]],
    *,
    audio_root: Path,
    limit: int,
) -> list[dict[str, Any]]:
    """Select scannable, one-utterance historical cases without inventing alignment."""
    candidates: list[dict[str, Any]] = []
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
        candidates.append(
            {
                "run_id": run_id,
                "utterance_id": utterance_id,
                "created_at": event.get("created_at"),
                "audio_path": _display_path(audio_path),
                "groq_text": str(event.get("source_text") or ""),
                "avg_logprob": event.get("avg_logprob"),
                "no_speech_prob": event.get("no_speech_prob"),
                "forced": bool(event.get("forced")),
                "quality_severity": event.get("quality_severity"),
                "quality_flags": event.get("quality_flags") or [],
                "risk": _risk(event),
            }
        )
    candidates.sort(key=lambda row: row["risk"], reverse=True)
    for row in candidates:
        row.pop("risk", None)
    return candidates[: max(0, limit)]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _normalized(text: str) -> str:
    return re.sub(r"\W+", "", text or "", flags=re.UNICODE).lower()


def run_scout(
    candidates: list[dict[str, Any]],
    *,
    generate: Callable[[np.ndarray], str],
    project_root: Path = PROJECT_ROOT,
) -> list[dict[str, Any]]:
    results = []
    for candidate in candidates:
        audio_path = Path(candidate["audio_path"])
        if not audio_path.is_absolute():
            audio_path = project_root / audio_path
        audio, rate = sf.read(audio_path, dtype="float32", always_2d=False)
        if rate != 16000 or audio.ndim != 1:
            raise ValueError(f"expected 16 kHz mono audio: {audio_path}")
        started = time.monotonic()
        raw = generate(audio)
        latency_ms = round((time.monotonic() - started) * 1000, 1)
        sensevoice = TAG_RE.sub("", raw).strip()
        similarity = difflib.SequenceMatcher(
            None, _normalized(candidate["groq_text"]), _normalized(sensevoice)
        ).ratio()
        results.append(
            {
                **candidate,
                "audio_sha256": _sha256(audio_path),
                "audio_seconds": round(len(audio) / rate, 3),
                "sensevoice_text": sensevoice,
                "sensevoice_raw": raw,
                "similarity": round(similarity, 3),
                "disagreement": round(1.0 - similarity, 3),
                "latency_ms": latency_ms,
            }
        )
    return results


def build_report(candidates: list[dict[str, Any]], results: list[dict[str, Any]]) -> dict[str, Any]:
    disagreements = [float(row["disagreement"]) for row in results]
    return {
        "schema": 1,
        "method": "offline_historical_single_utterance_scout",
        "candidate_count": len(candidates),
        "executed_count": len(results),
        "mean_disagreement": round(sum(disagreements) / len(disagreements), 3) if disagreements else None,
        "high_disagreement_ge_0_5": sum(value >= 0.5 for value in disagreements),
        "ground_truth_count": 0,
        "measured_rescues": None,
        "measured_false_corrections": None,
        "live_shadow_decision": "no-go",
        "decision_reason": (
            "Engine disagreement is a candidate-generation signal, not correctness. "
            "No exact heard-source ground truth exists for these historical WAVs."
        ),
        "candidates": candidates,
        "results": results,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a bounded offline SenseVoice historical-WAV scout.")
    parser.add_argument("--logs", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--audio-root", type=Path, default=DEFAULT_AUDIO_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--limit", type=int, default=24)
    parser.add_argument("--execute", action="store_true", help="Load SenseVoice and transcribe selected WAVs.")
    parser.add_argument("--model", default="iic/SenseVoiceSmall")
    parser.add_argument("--device", default="cuda")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    candidates = select_candidates(
        _events(sorted(args.logs.glob("runtime_events_*.jsonl"))),
        audio_root=args.audio_root,
        limit=args.limit,
    )
    results: list[dict[str, Any]] = []
    if args.execute and candidates:
        from funasr import AutoModel

        model = AutoModel(
            model=args.model,
            trust_remote_code=True,
            device=args.device,
            disable_update=True,
        )

        def generate(audio: np.ndarray) -> str:
            generated = model.generate(
                input=audio, cache={}, language="ko", use_itn=True, batch_size_s=60
            )
            return str(generated[0].get("text") or "") if generated else ""

        results = run_scout(candidates, generate=generate)
    report = build_report(candidates, results)
    report["model"] = args.model
    report["device"] = args.device if args.execute else "not_loaded"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(candidates)} candidates / {len(results)} replays to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
