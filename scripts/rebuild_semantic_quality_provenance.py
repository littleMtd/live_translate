from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = PROJECT_ROOT / "data" / "semantic_quality_evidence_20260802.json"
DEFAULT_EVENTS = PROJECT_ROOT / "logs" / "runtime_events_20260802.jsonl"
DEFAULT_AUDIO_ROOT = PROJECT_ROOT / "logs" / "audio_dump"
TARGET_RUNTIME_SCHEMA = 3
TARGET_MANIFEST_SCHEMA = 2

_FROZEN_RUNTIME_FIELDS = {
    "runtime_created_at_utc": "created_at",
    "profile_id": "profile_id",
    "status": "status",
    "source_text": "source_text",
    "target_text": "target_text",
}


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def read_runtime_events(paths: Iterable[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        with path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid runtime JSON at {path}:{line_no}: {exc}") from exc
                if not isinstance(event, dict):
                    raise ValueError(f"runtime row is not an object at {path}:{line_no}")
                rows.append({"path": str(path), "line_no": line_no, "event": event})
    return rows


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for text in (str(item).strip() for item in value) if text]


def _sequence_id(value: Any, *, context: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid translation sequence ID for {context}: {value!r}") from exc


def _runtime_indexes(
    rows: list[dict[str, Any]],
) -> tuple[
    dict[tuple[str, int], list[dict[str, Any]]],
    dict[tuple[str, str], list[dict[str, Any]]],
]:
    translations: dict[tuple[str, int], list[dict[str, Any]]] = {}
    successful_stt: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        event = row["event"]
        if event.get("schema_version") != TARGET_RUNTIME_SCHEMA:
            continue
        run_id = str(event.get("run_id") or "")
        if not run_id:
            continue
        event_type = event.get("event_type")
        if event_type == "translation":
            sequence_id = _sequence_id(event.get("sequence_id"), context=f"runtime {run_id}")
            translations.setdefault((run_id, sequence_id), []).append(row)
        elif event_type == "stt" and event.get("status") == "success":
            utterance_id = str(event.get("utterance_id") or "")
            if utterance_id:
                successful_stt.setdefault((run_id, utterance_id), []).append(row)
    return translations, successful_stt


def _display_path(path: Path, project_root: Path) -> str:
    resolved = path.resolve(strict=False)
    try:
        return resolved.relative_to(project_root.resolve(strict=False)).as_posix()
    except ValueError:
        return resolved.as_posix()


def _effective_sources(event: dict[str, Any]) -> list[tuple[str, str]]:
    ordered: list[tuple[str, str]] = []
    seen: set[str] = set()
    for source_kind, field in (
        ("current", "source_utterance_ids"),
        ("evidence", "evidence_source_utterance_ids"),
    ):
        for utterance_id in _string_list(event.get(field)):
            if utterance_id in seen:
                continue
            seen.add(utterance_id)
            ordered.append((utterance_id, source_kind))
    return ordered


def _validate_frozen_ref(runtime_ref: dict[str, Any], event: dict[str, Any]) -> None:
    identity = (
        f"{runtime_ref.get('run_id')}#"
        f"{runtime_ref.get('translation_sequence_id')}"
    )
    for frozen_field, runtime_field in _FROZEN_RUNTIME_FIELDS.items():
        if runtime_ref.get(frozen_field) != event.get(runtime_field):
            raise ValueError(
                f"frozen runtime field mismatch for {identity}: "
                f"{frozen_field}={runtime_ref.get(frozen_field)!r}, "
                f"runtime {runtime_field}={event.get(runtime_field)!r}"
            )


def _audio_ref(
    *,
    run_id: str,
    utterance_id: str,
    source_kind: str,
    successful_stt: dict[tuple[str, str], list[dict[str, Any]]],
    audio_root: Path,
    project_root: Path,
) -> dict[str, Any]:
    matches = successful_stt.get((run_id, utterance_id), [])
    if len(matches) != 1:
        raise ValueError(
            f"expected one successful STT join for {run_id}/{utterance_id}, "
            f"found {len(matches)}"
        )
    stt_event = matches[0]["event"]
    wav_path = audio_root / run_id / f"{utterance_id}.wav"
    if not wav_path.is_file():
        raise ValueError(f"missing WAV for {run_id}/{utterance_id}: {wav_path}")
    return {
        "utterance_id": utterance_id,
        "source_kind": source_kind,
        "stt_join_status": "joined_unique",
        "stt_status": str(stt_event.get("status") or ""),
        "wav_path": _display_path(wav_path, project_root),
        "wav_status": "exists",
    }


def rebuild_provenance(
    manifest: dict[str, Any],
    runtime_rows: list[dict[str, Any]],
    *,
    audio_root: Path,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    """Refresh frozen runtime attribution without changing selection decisions."""
    rebuilt = copy.deepcopy(manifest)
    annotations = rebuilt.get("annotations")
    if not isinstance(annotations, list):
        raise ValueError("provenance manifest must contain an annotations list")

    translations, successful_stt = _runtime_indexes(runtime_rows)
    missing_selected_annotations = 0

    for annotation in annotations:
        if not isinstance(annotation, dict):
            raise ValueError("annotation entry must be an object")
        timestamp_matches = annotation.get("timestamp_matches")
        if not isinstance(timestamp_matches, list):
            raise ValueError(
                f"annotation {annotation.get('annotation_id')} has no timestamp_matches list"
            )
        selected_missing = False
        for timestamp_match in timestamp_matches:
            if not isinstance(timestamp_match, dict):
                raise ValueError("timestamp match must be an object")
            runtime_refs = timestamp_match.get("runtime_refs")
            if not isinstance(runtime_refs, list):
                raise ValueError("timestamp match has no runtime_refs list")
            for runtime_ref in runtime_refs:
                if not isinstance(runtime_ref, dict):
                    raise ValueError("runtime reference must be an object")
                run_id = str(runtime_ref.get("run_id") or "")
                sequence_id = _sequence_id(
                    runtime_ref.get("translation_sequence_id"),
                    context=f"annotation {annotation.get('annotation_id')}",
                )
                matches = translations.get((run_id, sequence_id), [])
                if len(matches) != 1:
                    raise ValueError(
                        f"expected one schema-v3 translation for {run_id}#{sequence_id}, "
                        f"found {len(matches)}"
                    )
                event = matches[0]["event"]
                _validate_frozen_ref(runtime_ref, event)

                current_ids = _string_list(event.get("source_utterance_ids"))
                evidence_ids = _string_list(event.get("evidence_source_utterance_ids"))
                runtime_ref["source_utterance_ids"] = current_ids
                runtime_ref["evidence_source_utterance_ids"] = evidence_ids
                effective_sources = _effective_sources(event)
                runtime_ref["audio_refs"] = [
                    _audio_ref(
                        run_id=run_id,
                        utterance_id=utterance_id,
                        source_kind=source_kind,
                        successful_stt=successful_stt,
                        audio_root=audio_root,
                        project_root=project_root,
                    )
                    for utterance_id, source_kind in effective_sources
                ]
                if runtime_ref.get("selection_status") == "selected" and not effective_sources:
                    selected_missing = True

        if selected_missing:
            missing_selected_annotations += 1
            annotation["provenance_status"] = "partial_selected_translation_missing_source_ids"
        else:
            annotation["provenance_status"] = "runtime_translation_candidates_linked"

    rebuilt["schema_version"] = TARGET_MANIFEST_SCHEMA
    rebuilt["direct_annotation_linked_missing_source_id_count"] = missing_selected_annotations
    matching_contract = rebuilt.get("matching_contract")
    if isinstance(matching_contract, dict):
        matching_contract["source_attribution"] = (
            "Frozen timestamp selections are preserved. Runtime provenance is rebuilt from "
            "current source IDs followed by unseen carry-forward evidence IDs; current wins "
            "when an utterance appears in both lists."
        )
    return rebuilt


def _serialized(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Rebuild schema-v3 runtime/STT/WAV provenance for an already-frozen "
            "semantic-quality selection manifest."
        )
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--events", type=Path, nargs="+", default=[DEFAULT_EVENTS])
    parser.add_argument("--audio-root", type=Path, default=DEFAULT_AUDIO_ROOT)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output", type=Path)
    mode.add_argument("--check", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        manifest = _read_json(args.manifest)
        rebuilt = rebuild_provenance(
            manifest,
            read_runtime_events(args.events),
            audio_root=args.audio_root,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Semantic-quality provenance rebuild failed: {exc}", file=sys.stderr)
        return 1

    output_text = _serialized(rebuilt)
    if args.check:
        if args.manifest.read_text(encoding="utf-8") != output_text:
            print(f"Provenance artifact is stale: {args.manifest}", file=sys.stderr)
            return 1
        print(f"Provenance artifact is current: {args.manifest}")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(output_text, encoding="utf-8")
    print(
        f"Wrote {len(rebuilt['annotations'])} annotations to {args.output}; "
        f"missing selected provenance="
        f"{rebuilt['direct_annotation_linked_missing_source_id_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
