from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SAMPLES = [
    PROJECT_ROOT / "logs" / "labeling_sample_phase0_eval_20260613_host_primary_pilot10.json",
    PROJECT_ROOT / "logs" / "labeling_sample_phase0_eval_20260613_host_primary_batch2_20.json",
]
DEFAULT_ANNOTATIONS = (
    PROJECT_ROOT / "logs" / "labeling_sample_phase0_eval_20260613_host_primary.annotations.json"
)
DEFAULT_OUTPUT = PROJECT_ROOT / "scratch" / "analysis" / "phase0_replay_manifest_20260624.json"
SOURCE_ROUTING_TAGS = {
    "audio_source_mismatch",
    "clip_or_other_speaker",
    "host_over_clip",
    "multi_streamer",
    "wrong_speaker_selected",
}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"failed to read JSON {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object in {path}")
    return data


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


def _ground_truth_status(annotation: dict[str, Any]) -> str:
    if str(annotation.get("heard_source_text") or "").strip():
        return "exact_heard_source"
    if str(annotation.get("notes") or "").strip():
        return "correction_or_note_only"
    return "human_judgment_only"


def _root_cause_group(annotation: dict[str, Any], sample: dict[str, Any]) -> str:
    label = str(annotation.get("label") or "")
    speaker_tags = set(_string_list(annotation.get("speaker_source_tags")))
    if label == "ok":
        return "control_ok"
    if label == "unclear":
        return "unclear"
    if label == "a_translation_error":
        if str(sample.get("status") or "") == "filtered" or sample.get("target_text") is None:
            return "filter_policy"
        return "translation"
    if label in {"b_stt_error", "both"}:
        if speaker_tags & SOURCE_ROUTING_TAGS:
            return "source_routing"
        if "host_only" in speaker_tags:
            return "clean_host_stt"
        return "stt_unverified"
    raise ValueError(f"unsupported or empty label: {label!r}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _display_path(path: Path, project_root: Path) -> str:
    resolved = path.resolve(strict=False)
    try:
        return str(resolved.relative_to(project_root.resolve(strict=False)))
    except ValueError:
        return str(resolved)


def build_replay_manifest(
    *,
    sample_paths: list[Path],
    annotation_path: Path,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    if not sample_paths:
        raise ValueError("at least one sample file is required")

    annotations_data = _read_json(annotation_path)
    annotations = annotations_data.get("annotations")
    if not isinstance(annotations, dict):
        raise ValueError("annotation file must contain an annotations object")

    cases: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    speaker_policies: set[str] = set()
    label_counts: Counter[str] = Counter()
    group_counts: Counter[str] = Counter()

    for sample_path in sample_paths:
        sample_data = _read_json(sample_path)
        speaker_policy = str(sample_data.get("speaker_policy") or "")
        if speaker_policy:
            speaker_policies.add(speaker_policy)
        samples = sample_data.get("samples")
        if not isinstance(samples, list):
            raise ValueError(f"sample file must contain a samples list: {sample_path}")

        for sample in samples:
            if not isinstance(sample, dict):
                raise ValueError(f"sample entry must be an object: {sample_path}")
            sample_id = str(sample.get("sample_id") or "")
            if not sample_id:
                raise ValueError(f"sample entry has no sample_id: {sample_path}")
            if sample_id in seen_ids:
                raise ValueError(f"duplicate sample_id across replay inputs: {sample_id}")
            seen_ids.add(sample_id)

            annotation = annotations.get(sample_id)
            if not isinstance(annotation, dict) or not str(annotation.get("label") or ""):
                raise ValueError(f"missing completed annotation for {sample_id}")
            group = _root_cause_group(annotation, sample)
            ground_truth_status = _ground_truth_status(annotation)

            chunks = sample.get("source_chunks")
            if not isinstance(chunks, list) or not chunks:
                raise ValueError(f"sample has no source chunks: {sample_id}")
            audio_assets: list[dict[str, Any]] = []
            for chunk in chunks:
                if not isinstance(chunk, dict):
                    raise ValueError(f"invalid source chunk in {sample_id}")
                audio_path_text = str(chunk.get("audio_path") or "")
                if not audio_path_text:
                    raise ValueError(f"source chunk has no audio_path in {sample_id}")
                audio_path = Path(audio_path_text)
                if not audio_path.is_file():
                    raise ValueError(f"missing source audio for {sample_id}: {audio_path}")
                audio_assets.append(
                    {
                        "utterance_id": str(chunk.get("utterance_id") or ""),
                        "chunk_role": str(chunk.get("chunk_role") or ""),
                        "source_kind": str(chunk.get("source_kind") or ""),
                        "audio_path": _display_path(audio_path, project_root),
                        "size_bytes": audio_path.stat().st_size,
                        "sha256": _sha256(audio_path),
                    }
                )

            label = str(annotation["label"])
            label_counts[label] += 1
            group_counts[group] += 1
            cases.append(
                {
                    "sample_id": sample_id,
                    "root_cause_group": group,
                    "ground_truth_status": ground_truth_status,
                    "annotation": annotation,
                    "audio_assets": audio_assets,
                    # Preserve attribution, carry-forward, diagnostics, and translation metadata verbatim.
                    "sample": sample,
                }
            )

    if speaker_policies != {"host-primary"}:
        raise ValueError(f"replay inputs must use host-primary policy: {sorted(speaker_policies)}")

    return {
        "phase0_replay_manifest_schema": 2,
        "speaker_policy": "host-primary",
        "sample_files": [_display_path(path, project_root) for path in sample_paths],
        "annotation_file": _display_path(annotation_path, project_root),
        "case_count": len(cases),
        "label_counts": dict(sorted(label_counts.items())),
        "root_cause_group_counts": dict(sorted(group_counts.items())),
        "cases": cases,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Freeze labeled Phase 0 cases into a replay manifest.")
    parser.add_argument("samples", nargs="*", type=Path, default=DEFAULT_SAMPLES)
    parser.add_argument("--annotations", type=Path, default=DEFAULT_ANNOTATIONS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        manifest = build_replay_manifest(
            sample_paths=args.samples,
            annotation_path=args.annotations,
        )
    except ValueError as exc:
        print(f"Phase 0 replay manifest failed: {exc}", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"Wrote {manifest['case_count']} replay cases to {args.output} "
        f"with groups {manifest['root_cause_group_counts']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
