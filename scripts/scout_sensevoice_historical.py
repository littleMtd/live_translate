from __future__ import annotations

import argparse
import collections
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
DEFAULT_OUTPUT = PROJECT_ROOT / "scratch" / "analysis" / "sensevoice_selective_replay_20260725.json"
TAG_RE = re.compile(r"<\|[^>]*\|>")
LOW_LOGPROB_TRIGGER = -0.7
FORCED_CUT_REASONS = {
    "hard_max",
    "soft_max",
    "soft_max_pause",
    "forced_blob",
    "forced_prefix",
    "forced_gap_prefix",
}


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


def _risk(event: dict[str, Any], triggers: list[str]) -> tuple[int, float, str]:
    trigger_rank = {
        "compression_ratio": 4,
        "forced_cut": 3,
        "low_logprob": 2,
    }
    rank = max((trigger_rank.get(trigger, 0) for trigger in triggers), default=0)
    avg_logprob = event.get("avg_logprob")
    confidence_risk = -float(avg_logprob) if isinstance(avg_logprob, (int, float)) else 0.0
    return rank, confidence_risk, str(event.get("created_at") or "")


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
    """Select risky original WAVs and compare text only when alignment is structural.

    STT runtime events intentionally do not persist transcript text. A translation
    event may contain a splitter prefix or residual rather than the exact transcript
    for one WAV, even when its terminal utterance id matches. Therefore source text
    is admitted as a Groq comparison only for a single-source/no-evidence event whose
    raw text length equals the STT event's recorded text length.
    """
    all_events = list(events)
    related_translations: dict[tuple[str, str], list[dict[str, Any]]] = collections.defaultdict(list)
    forced_sentence_reasons: dict[tuple[str, str], set[str]] = collections.defaultdict(set)
    for event in all_events:
        utterance_ids = event.get("source_utterance_ids")
        if (
            event.get("event_type") == "translation"
            and event.get("status") == "success"
            and isinstance(utterance_ids, list)
        ):
            run_id = str(event.get("run_id") or "")
            for utterance_id in utterance_ids:
                related_translations[(run_id, str(utterance_id or ""))].append(event)
        if event.get("event_type") == "sentence" and (
            bool(event.get("forced"))
            or str(event.get("cut_reason") or "") in FORCED_CUT_REASONS
            or str(event.get("cut_reason") or "").startswith("forced")
        ):
            run_id = str(event.get("run_id") or "")
            sentence_ids = utterance_ids
            if not isinstance(sentence_ids, list):
                sentence_ids = [event.get("utterance_id")]
            for utterance_id in sentence_ids:
                if utterance_id:
                    forced_sentence_reasons[(run_id, str(utterance_id))].add(
                        str(event.get("cut_reason") or "forced")
                    )

    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for event in all_events:
        if (
            event.get("event_type") != "stt"
            or event.get("status") not in {"success", "filtered"}
            or event.get("request_sent") is False
        ):
            continue
        run_id = str(event.get("run_id") or "")
        utterance_id = str(event.get("utterance_id") or "")
        key = run_id, utterance_id
        audio_path = audio_root / run_id / f"{utterance_id}.wav"
        if not run_id or not utterance_id or key in seen or not audio_path.is_file():
            continue

        triggers: list[str] = []
        avg_logprob = event.get("avg_logprob")
        if isinstance(avg_logprob, (int, float)) and float(avg_logprob) <= LOW_LOGPROB_TRIGGER:
            triggers.append("low_logprob")
        if str(event.get("reason") or "") == "compression_ratio":
            triggers.append("compression_ratio")
        cut_reason = str(event.get("vad_cut_reason") or "")
        related = related_translations.get(key, [])
        translation = next(
            (
                row
                for row in related
                if row.get("source_utterance_ids") == [utterance_id]
                and int(row.get("source_count") or 1) == 1
                and "evidence_source_utterance_ids" in row
                and isinstance(row.get("evidence_source_utterance_ids"), list)
                and not row["evidence_source_utterance_ids"]
                and str(row.get("source_text") or "").strip()
            ),
            None,
        )
        translation_cut_reason = str((translation or {}).get("cut_reason") or "")
        if (
            cut_reason in FORCED_CUT_REASONS
            or cut_reason.startswith("forced")
            or key in forced_sentence_reasons
            or bool((translation or {}).get("forced"))
            or translation_cut_reason in FORCED_CUT_REASONS
            or translation_cut_reason.startswith("forced")
        ):
            triggers.append("forced_cut")
        if not triggers:
            continue

        source_text = str((translation or {}).get("source_text") or "")
        stt_text_len = event.get("text_len")
        aligned = (
            bool(source_text)
            and isinstance(stt_text_len, int)
            and len(source_text) == stt_text_len
        )
        if translation is None and any(
            "evidence_source_utterance_ids" not in row for row in related
        ):
            alignment = "evidence_attribution_unavailable"
        elif translation is None and any(
            isinstance(row.get("evidence_source_utterance_ids"), list)
            and row["evidence_source_utterance_ids"]
            for row in related
        ):
            alignment = "evidence_bearing_translation"
        elif translation is None and related:
            alignment = "multi_source_or_ineligible_translation"
        elif translation is None:
            alignment = "translation_unavailable"
        elif not isinstance(stt_text_len, int):
            alignment = "stt_text_length_unavailable"
        elif len(source_text) != stt_text_len:
            alignment = "text_length_mismatch"
        else:
            alignment = "single_source_length_match"

        seen.add(key)
        candidates.append(
            {
                "run_id": run_id,
                "utterance_id": utterance_id,
                "created_at": event.get("created_at"),
                "audio_path": _display_path(audio_path),
                "trigger_reasons": triggers,
                "stt_status": event.get("status"),
                "stt_reason": event.get("reason"),
                "stt_text_len": stt_text_len,
                "vad_cut_reason": cut_reason,
                "sentence_forced_cut_reasons": sorted(forced_sentence_reasons.get(key, set())),
                "groq_text": source_text if aligned else "",
                "groq_text_alignment": alignment,
                "avg_logprob": event.get("avg_logprob"),
                "no_speech_prob": event.get("no_speech_prob"),
                "forced": "forced_cut" in triggers,
                "profile_id": event.get("profile_id"),
                "risk": _risk(event, triggers),
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
        groq_text = str(candidate.get("groq_text") or "")
        similarity = (
            difflib.SequenceMatcher(
                None, _normalized(groq_text), _normalized(sensevoice)
            ).ratio()
            if groq_text
            else None
        )
        results.append(
            {
                **candidate,
                "audio_sha256": _sha256(audio_path),
                "audio_seconds": round(len(audio) / rate, 3),
                "sensevoice_text": sensevoice,
                "sensevoice_raw": raw,
                "similarity": round(similarity, 3) if similarity is not None else None,
                "disagreement": round(1.0 - similarity, 3) if similarity is not None else None,
                "latency_ms": latency_ms,
            }
        )
    return results


def build_report(candidates: list[dict[str, Any]], results: list[dict[str, Any]]) -> dict[str, Any]:
    disagreements = [
        float(row["disagreement"])
        for row in results
        if isinstance(row.get("disagreement"), (int, float))
    ]
    triggers = collections.Counter(
        trigger
        for candidate in candidates
        for trigger in candidate.get("trigger_reasons", [])
    )
    alignments = collections.Counter(
        str(candidate.get("groq_text_alignment") or "unknown")
        for candidate in candidates
    )
    return {
        "schema": 2,
        "method": "offline_selective_secondary_stt_replay",
        "candidate_count": len(candidates),
        "executed_count": len(results),
        "comparison_count": len(disagreements),
        "candidate_triggers": dict(sorted(triggers.items())),
        "groq_text_alignment": dict(sorted(alignments.items())),
        "mean_disagreement": round(sum(disagreements) / len(disagreements), 3) if disagreements else None,
        "high_disagreement_ge_0_5": sum(value >= 0.5 for value in disagreements),
        "ground_truth_count": 0,
        "measured_rescues": None,
        "measured_false_corrections": None,
        "live_shadow_decision": "no-go",
        "decision_reason": (
            "Replay uses original WAV evidence, but engine disagreement and non-empty "
            "filtered-case output are not correctness. No exact heard-source ground "
            "truth exists for these historical WAVs."
        ),
        "alignment_rule": (
            "Groq comparison text is admitted only when a successful single-source, "
            "no-evidence translation has the same raw length as the STT event."
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
