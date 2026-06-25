from __future__ import annotations

import hashlib
import json
import math
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import soundfile as sf


ROUTING_SPAN_SCHEMA = 1
SOURCE_CLASSES = (
    "host",
    "content_other",
    "alert_tts",
    "mixed",
    "unrelated",
    "uncertain",
)
ROUTING_ACTIONS = (
    "translate",
    "extract_host",
    "extract_content",
    "suppress",
    "context_only",
    "exclude",
)
ANNOTATION_STATUSES = ("draft", "complete")
COVERAGE_TOLERANCE_SECONDS = 0.05


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"failed to read replay manifest {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
        raise ValueError("replay manifest must contain a cases list")
    return data


def _audio_duration(asset: dict[str, Any], sample: dict[str, Any], project_root: Path) -> float:
    utterance_id = str(asset.get("utterance_id") or "")
    chunks = sample.get("source_chunks")
    if isinstance(chunks, list):
        for chunk in chunks:
            if not isinstance(chunk, dict) or str(chunk.get("utterance_id") or "") != utterance_id:
                continue
            value = chunk.get("stt_audio_seconds")
            if isinstance(value, (int, float)) and value > 0:
                return round(float(value), 3)
    audio_path = Path(str(asset.get("audio_path") or ""))
    if not audio_path.is_absolute():
        audio_path = project_root / audio_path
    try:
        return round(float(sf.info(audio_path).duration), 3)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"failed to read audio duration: {audio_path}") from exc


def build_routing_tasks(manifest: dict[str, Any], *, project_root: Path) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for case in manifest["cases"]:
        if not isinstance(case, dict) or case.get("root_cause_group") != "source_routing":
            continue
        sample = case.get("sample")
        assets = case.get("audio_assets")
        if not isinstance(sample, dict) or not isinstance(assets, list) or not assets:
            raise ValueError(f"invalid source-routing case: {case.get('sample_id')}")
        public_assets: list[dict[str, Any]] = []
        for index, asset in enumerate(assets, start=1):
            if not isinstance(asset, dict):
                raise ValueError(f"invalid audio asset in {case.get('sample_id')}")
            public_assets.append(
                {
                    "utterance_id": str(asset.get("utterance_id") or ""),
                    "chunk_role": str(asset.get("chunk_role") or ""),
                    "source_kind": str(asset.get("source_kind") or ""),
                    "duration_seconds": _audio_duration(asset, sample, project_root),
                    "audio_url": f"/audio/{case['sample_id']}-{index}.wav",
                    "runtime_audio_path": str(asset.get("audio_path") or ""),
                    "expected_size_bytes": asset.get("size_bytes"),
                    "expected_sha256": asset.get("sha256"),
                }
            )
        tasks.append(
            {
                "sample_id": str(case.get("sample_id") or ""),
                "run_id": sample.get("run_id"),
                "sequence_id": sample.get("sequence_id"),
                "source_text": sample.get("source_text"),
                "target_text": sample.get("target_text"),
                "speaker_source_tags": case.get("annotation", {}).get("speaker_source_tags", []),
                "context_tags": case.get("annotation", {}).get("context_tags", []),
                "source_utterance_ids": sample.get("source_utterance_ids", []),
                "evidence_source_utterance_ids": sample.get("evidence_source_utterance_ids", []),
                "source_chunk_usages": sample.get("source_chunk_usages", []),
                "audio_assets": public_assets,
            }
        )
    if not tasks:
        raise ValueError("manifest contains no source_routing cases")
    return tasks


def _number(value: Any, name: str) -> float:
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{name} must be a finite number")
    return round(float(value), 3)


def normalize_spans(spans: Any, *, duration_seconds: float) -> list[dict[str, Any]]:
    if not isinstance(spans, list):
        raise ValueError("spans must be a list")
    normalized: list[dict[str, Any]] = []
    for index, span in enumerate(spans, start=1):
        if not isinstance(span, dict):
            raise ValueError("span must be an object")
        start = _number(span.get("start_seconds"), "start_seconds")
        end = _number(span.get("end_seconds"), "end_seconds")
        if start < 0 or end <= start:
            raise ValueError("span must satisfy 0 <= start < end")
        if end > duration_seconds + COVERAGE_TOLERANCE_SECONDS:
            raise ValueError(f"span exceeds audio duration {duration_seconds:.3f}s")
        source_class = str(span.get("source_class") or "")
        routing_action = str(span.get("routing_action") or "")
        if source_class not in SOURCE_CLASSES:
            raise ValueError(f"unknown source_class: {source_class}")
        if routing_action not in ROUTING_ACTIONS:
            raise ValueError(f"unknown routing_action: {routing_action}")
        normalized.append(
            {
                "span_id": str(span.get("span_id") or f"span-{index}"),
                "start_seconds": start,
                "end_seconds": min(end, duration_seconds),
                "source_class": source_class,
                "routing_action": routing_action,
                "notes": str(span.get("notes") or "").strip(),
            }
        )
    normalized.sort(key=lambda item: (item["start_seconds"], item["end_seconds"]))
    previous_end = 0.0
    for span in normalized:
        if span["start_seconds"] < previous_end - COVERAGE_TOLERANCE_SECONDS:
            raise ValueError("spans must not overlap")
        previous_end = max(previous_end, span["end_seconds"])
    return normalized


def coverage_gaps(spans: list[dict[str, Any]], *, duration_seconds: float) -> list[dict[str, float]]:
    gaps: list[dict[str, float]] = []
    cursor = 0.0
    for span in spans:
        start = float(span["start_seconds"])
        end = float(span["end_seconds"])
        if start > cursor + COVERAGE_TOLERANCE_SECONDS:
            gaps.append({"start_seconds": round(cursor, 3), "end_seconds": round(start, 3)})
        cursor = max(cursor, end)
    if duration_seconds > cursor + COVERAGE_TOLERANCE_SECONDS:
        gaps.append(
            {"start_seconds": round(cursor, 3), "end_seconds": round(duration_seconds, 3)}
        )
    return gaps


class RoutingAnnotationStore:
    def __init__(
        self,
        *,
        path: Path,
        manifest_path: Path,
        tasks: list[dict[str, Any]],
    ):
        self.path = path
        self.manifest_path = manifest_path
        self.manifest_sha256 = sha256_file(manifest_path)
        self.tasks = {str(task["sample_id"]): task for task in tasks}
        self.lock = threading.Lock()
        self._data = self._load()

    def _empty_data(self) -> dict[str, Any]:
        now = _now_iso()
        return {
            "routing_span_annotation_schema": ROUTING_SPAN_SCHEMA,
            "manifest_path": str(self.manifest_path.resolve(strict=False)),
            "manifest_sha256": self.manifest_sha256,
            "created_at": now,
            "updated_at": now,
            "source_classes": list(SOURCE_CLASSES),
            "routing_actions": list(ROUTING_ACTIONS),
            "annotations": {},
        }

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty_data()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid routing annotation file {self.path}: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("annotations"), dict):
            raise ValueError("routing annotation file must contain an annotations object")
        if data.get("manifest_sha256") != self.manifest_sha256:
            raise ValueError("routing annotation manifest fingerprint does not match current manifest")
        return data

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return json.loads(json.dumps(self._data, ensure_ascii=False))

    def update(self, payload: dict[str, Any]) -> dict[str, Any]:
        sample_id = str(payload.get("sample_id") or "")
        task = self.tasks.get(sample_id)
        if task is None:
            raise ValueError(f"unknown sample_id: {sample_id}")
        status = str(payload.get("status") or "draft")
        if status not in ANNOTATION_STATUSES:
            raise ValueError(f"unknown status: {status}")
        raw_assets = payload.get("assets")
        if not isinstance(raw_assets, dict):
            raise ValueError("assets must be an object keyed by utterance_id")

        normalized_assets: dict[str, Any] = {}
        expected_ids = {str(asset["utterance_id"]) for asset in task["audio_assets"]}
        unknown_ids = set(raw_assets) - expected_ids
        if unknown_ids:
            raise ValueError(f"unknown audio assets for {sample_id}: {sorted(unknown_ids)}")
        for asset in task["audio_assets"]:
            utterance_id = str(asset["utterance_id"])
            raw_asset = raw_assets.get(utterance_id, {})
            if not isinstance(raw_asset, dict):
                raise ValueError(f"invalid asset annotation for {utterance_id}")
            duration = float(asset["duration_seconds"])
            spans = normalize_spans(raw_asset.get("spans", []), duration_seconds=duration)
            gaps = coverage_gaps(spans, duration_seconds=duration)
            if status == "complete" and gaps:
                raise ValueError(f"incomplete time coverage for {utterance_id}: {gaps}")
            normalized_assets[utterance_id] = {
                "duration_seconds": duration,
                "spans": spans,
                "coverage_gaps": gaps,
            }

        annotation = {
            "status": status,
            "notes": str(payload.get("notes") or "").strip(),
            "assets": normalized_assets,
            "updated_at": _now_iso(),
        }
        with self.lock:
            self._data["annotations"][sample_id] = annotation
            self._data["updated_at"] = annotation["updated_at"]
            self._write_locked()
            return json.loads(json.dumps(annotation, ensure_ascii=False))

    def _write_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        temp_path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(self.path)
