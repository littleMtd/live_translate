from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable

import numpy as np
import soundfile as sf


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = PROJECT_ROOT / ".analysis-tmp" / "phase0_replay_manifest_20260624.json"
DEFAULT_OUTPUT = PROJECT_ROOT / ".analysis-tmp" / "phase0_sensevoice_shadow_20260624.json"
DEFAULT_GROUPS = ("clean_host_stt", "stt_unverified")
SENSEVOICE_TAG_RE = re.compile(r"<\|[^>]*\|>")


def clean_sensevoice_text(raw_text: str) -> tuple[str, list[str]]:
    tags = SENSEVOICE_TAG_RE.findall(raw_text)
    text = SENSEVOICE_TAG_RE.sub("", raw_text).strip()
    return text, tags


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"failed to read replay manifest {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
        raise ValueError("replay manifest must contain a cases list")
    return data


def select_cases(
    manifest: dict[str, Any],
    *,
    groups: set[str],
    case_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    selected = [
        case
        for case in manifest["cases"]
        if isinstance(case, dict)
        and str(case.get("root_cause_group") or "") in groups
        and (case_ids is None or str(case.get("sample_id") or "") in case_ids)
    ]
    if case_ids is not None:
        found = {str(case.get("sample_id") or "") for case in selected}
        missing = sorted(case_ids - found)
        if missing:
            raise ValueError(f"requested cases are not eligible for selected groups: {missing}")
    return selected


def _chronological_assets(case: dict[str, Any]) -> list[dict[str, Any]]:
    assets = case.get("audio_assets")
    sample = case.get("sample")
    if not isinstance(assets, list) or not isinstance(sample, dict):
        raise ValueError(f"invalid replay case: {case.get('sample_id')}")
    chunks = sample.get("source_chunks")
    if not isinstance(chunks, list):
        return assets
    event_lines = {
        str(chunk.get("utterance_id") or ""): chunk.get("stt_event_line")
        for chunk in chunks
        if isinstance(chunk, dict)
    }
    indexed = list(enumerate(assets))
    indexed.sort(
        key=lambda item: (
            event_lines.get(str(item[1].get("utterance_id") or ""))
            if isinstance(event_lines.get(str(item[1].get("utterance_id") or "")), int)
            else float("inf"),
            item[0],
        )
    )
    return [asset for _, asset in indexed]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_audio_asset(asset: dict[str, Any], audio_path: Path) -> None:
    expected_size = asset.get("size_bytes")
    expected_sha256 = str(asset.get("sha256") or "")
    if not isinstance(expected_size, int) or not expected_sha256:
        raise ValueError(f"audio asset has no frozen fingerprint: {audio_path}")
    actual_size = audio_path.stat().st_size
    if actual_size != expected_size:
        raise ValueError(
            f"audio size changed since manifest build: {audio_path} "
            f"(expected {expected_size}, got {actual_size})"
        )
    actual_sha256 = _sha256(audio_path)
    if actual_sha256 != expected_sha256:
        raise ValueError(f"audio sha256 changed since manifest build: {audio_path}")


def _groq_diagnostics(sample: dict[str, Any]) -> dict[str, Any]:
    chunks = sample.get("source_chunks")
    chunk_diagnostics = []
    if isinstance(chunks, list):
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            chunk_diagnostics.append(
                {
                    key: chunk.get(key)
                    for key in (
                        "utterance_id",
                        "chunk_role",
                        "source_kind",
                        "stt_status",
                        "stt_reason",
                        "stt_engine",
                        "stt_model",
                        "stt_audio_seconds",
                        "stt_latency_ms",
                        "avg_logprob",
                        "no_speech_prob",
                        "audio_context",
                    )
                }
            )
    return {
        "translation_cut_reason": sample.get("translation_cut_reason"),
        "sentence_cut_reason": sample.get("sentence_cut_reason"),
        "source_utterance_ids": sample.get("source_utterance_ids"),
        "evidence_source_utterance_ids": sample.get("evidence_source_utterance_ids"),
        "quality_score": sample.get("quality_score"),
        "quality_flags": sample.get("quality_flags"),
        "chunks": chunk_diagnostics,
    }


def replay_cases(
    cases: list[dict[str, Any]],
    *,
    generate: Callable[[np.ndarray], str],
    project_root: Path = PROJECT_ROOT,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for case in cases:
        chunk_results: list[dict[str, Any]] = []
        for asset in _chronological_assets(case):
            audio_path = Path(str(asset.get("audio_path") or ""))
            if not audio_path.is_absolute():
                audio_path = project_root / audio_path
            verify_audio_asset(asset, audio_path)
            audio, sample_rate = sf.read(audio_path, dtype="float32", always_2d=False)
            if sample_rate != 16000:
                raise ValueError(f"expected 16 kHz audio for {case['sample_id']}: {audio_path}")
            if audio.ndim != 1:
                raise ValueError(f"expected mono audio for {case['sample_id']}: {audio_path}")
            started = time.monotonic()
            raw_text = generate(audio)
            latency_ms = round((time.monotonic() - started) * 1000, 1)
            text, tags = clean_sensevoice_text(raw_text)
            chunk_results.append(
                {
                    "utterance_id": str(asset.get("utterance_id") or ""),
                    "source_kind": str(asset.get("source_kind") or "current"),
                    "audio_path": str(asset.get("audio_path") or ""),
                    "audio_seconds": round(len(audio) / sample_rate, 3),
                    "text": text,
                    "metadata_tags": tags,
                    "raw_text": raw_text,
                    "latency_ms": latency_ms,
                    "audio_fingerprint_verified": True,
                }
            )
        sample = case["sample"]
        current_text = " ".join(
            row["text"]
            for row in chunk_results
            if row["text"] and row["source_kind"] != "evidence"
        ).strip()
        evidence_text = " ".join(
            row["text"]
            for row in chunk_results
            if row["text"] and row["source_kind"] == "evidence"
        ).strip()
        output.append(
            {
                "sample_id": case["sample_id"],
                "root_cause_group": case["root_cause_group"],
                "ground_truth_status": case.get("ground_truth_status"),
                "groq_source_text": sample.get("source_text"),
                "groq_diagnostics": _groq_diagnostics(sample),
                "annotation": case.get("annotation"),
                "sensevoice_text": current_text,
                "sensevoice_current_text": current_text,
                "sensevoice_evidence_text": evidence_text,
                "sensevoice_chunks": chunk_results,
            }
        )
    return output


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay Phase 0 STT cases through SenseVoice offline.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--group", action="append", dest="groups", default=None)
    parser.add_argument("--case-id", action="append", dest="case_ids", default=None)
    parser.add_argument("--model", default="iic/SenseVoiceSmall")
    parser.add_argument("--device", default="cuda")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    groups = set(args.groups or DEFAULT_GROUPS)
    case_ids = set(args.case_ids) if args.case_ids else None
    try:
        manifest = _read_manifest(args.manifest)
        cases = select_cases(manifest, groups=groups, case_ids=case_ids)
        if not cases:
            raise ValueError("no replay cases matched the requested groups")

        from funasr import AutoModel

        model = AutoModel(
            model=args.model,
            trust_remote_code=True,
            device=args.device,
            disable_update=True,
        )

        def generate(audio: np.ndarray) -> str:
            result = model.generate(
                input=audio,
                cache={},
                language="ko",
                use_itn=True,
                batch_size_s=60,
            )
            if not result:
                return ""
            return str(result[0].get("text") or "")

        results = replay_cases(cases, generate=generate)
    except (OSError, ValueError) as exc:
        print(f"Phase 0 STT replay failed: {exc}", file=sys.stderr)
        return 1

    output = {
        "phase0_stt_replay_schema": 2,
        "manifest": str(args.manifest),
        "manifest_sha256": _sha256(args.manifest),
        "engine": "sensevoice",
        "model": args.model,
        "device": args.device,
        "groups": sorted(groups),
        "case_count": len(results),
        "cases": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(results)} SenseVoice replay cases to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
