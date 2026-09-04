from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
import tempfile
import wave
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


BUNDLE_SCHEMA_VERSION = 1
DEFAULT_MAX_PART_BYTES = 50 * 1024 * 1024
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_BASIC_AUTH = re.compile(r"(?i)(?:authorization\s*:\s*)?basic\s+[A-Za-z0-9+/=]+")
_DEEPL_AUTH = re.compile(r"(?i)\bDeepL-Auth-Key\s+[A-Za-z0-9._~+/=-]+")
_COMMON_API_TOKEN = re.compile(r"(?<![A-Za-z0-9])(?:(?:sk|gsk|xai)[-_][A-Za-z0-9_-]{12,}|AIza[A-Za-z0-9_-]{12,})")
_URL_CREDENTIALS = re.compile(r"(?i)(https?://)[^/@\s:]+:[^/@\s]+@")
_SCREENSHOT_KEYS = re.compile(r"(?:screenshot|screen_capture|image_(?:data|base64)|frame_base64)", re.I)
_INSTRUCTION = """This bundle contains one live_translate runtime session.
Use runtime_events*.jsonl as the source of truth.
CHATGPT_PROJECT_README.md and manifest.json are indexes/derived views.
Do not assume STT text is audio ground truth.
Distinguish STT, sentence assembly, translation, profile/context, and publication failures."""


@dataclass(frozen=True)
class SourceEvent:
    event: dict[str, Any]
    source_file: Path
    source_line: int
    ordinal: int


def _normalized_key(key: str) -> str:
    camel_split = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(key))
    return re.sub(r"[^a-z0-9]+", "_", camel_split.lower()).strip("_")


def _is_secret_key(key: str) -> bool:
    normalized = _normalized_key(key)
    segments = set(normalized.split("_"))
    if normalized in {"authorization", "cookie", "set_cookie", "password", "passwd", "secret", "credential", "credentials"}:
        return True
    if normalized.endswith(("_headers", "_header")) or normalized in {"header", "headers", "http_headers"}:
        return True
    if normalized.endswith(("_api_key", "_client_secret", "_access_token", "_refresh_token", "_auth_token", "_token", "_secret", "_password", "_passwd", "_credential", "_credentials", "_private_key")):
        return True
    if "secret" in segments or normalized.endswith("_key") or {"access", "key"}.issubset(segments):
        return True
    return normalized in {"api_key", "client_secret", "access_token", "refresh_token", "auth_token"}


def _json_dump(value: Any, *, pretty: bool = False) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        indent=2 if pretty else None,
        sort_keys=pretty,
        separators=None if pretty else (",", ":"),
    )


def sanitize_value(value: Any, *, project_root: Path | None = None, key: str = "") -> tuple[Any, int]:
    if _is_secret_key(str(key)):
        return "<REDACTED>", 1
    if _SCREENSHOT_KEYS.search(str(key)):
        return "<OMITTED_PRIVACY_MEDIA>", 1
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        count = 0
        for child_key, child in value.items():
            output[str(child_key)], child_count = sanitize_value(
                child, project_root=project_root, key=str(child_key)
            )
            count += child_count
        return output, count
    if isinstance(value, list):
        output = []
        count = 0
        for child in value:
            clean, child_count = sanitize_value(child, project_root=project_root)
            output.append(clean)
            count += child_count
        return output, count
    if isinstance(value, str):
        clean, count = _BEARER.subn("Bearer <REDACTED>", value)
        clean, basic_count = _BASIC_AUTH.subn("Authorization: Basic <REDACTED>", clean)
        clean, deep_count = _DEEPL_AUTH.subn("DeepL-Auth-Key <REDACTED>", clean)
        clean, token_count = _COMMON_API_TOKEN.subn("<REDACTED>", clean)
        count += basic_count + deep_count + token_count
        clean, url_count = _URL_CREDENTIALS.subn(r"\1<REDACTED>@", clean)
        count += url_count
        if project_root:
            root = str(project_root.resolve())
            if root:
                replaced = clean.replace(root, "<PROJECT_ROOT>").replace(root.replace("\\", "/"), "<PROJECT_ROOT>")
                if replaced != clean:
                    clean = replaced
        return clean, count
    return value, 0


def discover_event_files(log_dir: Path) -> list[Path]:
    return sorted(Path(log_dir).glob("runtime_events_*.jsonl"), key=lambda path: path.name)


def read_run_events(event_files: Iterable[Path], run_id: str) -> list[SourceEvent]:
    result: list[SourceEvent] = []
    ordinal = 0
    for path in sorted((Path(item) for item in event_files), key=lambda item: str(item)):
        with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
            for line_number, line in enumerate(handle, start=1):
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(event, dict) or str(event.get("run_id") or "") != run_id:
                    continue
                ordinal += 1
                result.append(SourceEvent(event, path, line_number, ordinal))
    return result


def list_runs(log_dir: Path) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for path in discover_event_files(log_dir):
        with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                run_id = str(event.get("run_id") or "") if isinstance(event, dict) else ""
                if run_id:
                    grouped.setdefault(run_id, []).append(event)
    rows = []
    for run_id, events in grouped.items():
        timestamps = sorted(str(event.get("created_at") or "") for event in events if event.get("created_at"))
        rows.append(
            {
                "run_id": run_id,
                "started_at": timestamps[0] if timestamps else "",
                "ended_at": timestamps[-1] if timestamps else "",
                "event_count": len(events),
                "run_kind": str(events[0].get("run_kind") or "legacy-live"),
                "run_complete": _has_terminal_event(events),
            }
        )
    return sorted(rows, key=lambda row: (row["started_at"], row["run_id"]), reverse=True)


def _has_terminal_event(events: Iterable[dict[str, Any]]) -> bool:
    terminal = {"stopped", "shutdown", "completed", "finished"}
    return any(
        str(event.get("event_type") or "") in {"runtime", "runtime_lifecycle", "pipeline"}
        and str(event.get("action") or event.get("status") or "").lower() in terminal
        for event in events
    )


def _parse_timestamp(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _event_ids(event: dict[str, Any]) -> list[str]:
    values: list[Any] = []
    for key in ("utterance_id", "source_utterance_id", "evidence_utterance_id"):
        if event.get(key):
            values.append(event[key])
    for key in ("source_utterance_ids", "evidence_source_utterance_ids", "utterance_ids"):
        if isinstance(event.get(key), list):
            values.extend(event[key])
    return list(dict.fromkeys(str(value) for value in values if str(value).strip()))


def _audio_duration(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate()
            return round(handle.getnframes() / rate, 4) if rate else None
    except (OSError, EOFError, wave.Error):
        return None


def _audio_index(
    rows: list[SourceEvent], *, audio_root: Path, run_id: str, project_root: Path
) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        event = row.event
        for utterance_id in _event_ids(event):
            item = by_id.setdefault(
                utterance_id,
                {
                    "run_id": run_id,
                    "utterance_id": utterance_id,
                    "original_wav_path": str(Path(audio_root) / run_id / f"{utterance_id}.wav"),
                    "duration_seconds": None,
                    "timestamp": str(event.get("created_at") or ""),
                    "source_event_ordinals": [],
                    "source_relationships": [],
                    "available": False,
                    "bundle_path": None,
                },
            )
            item["source_event_ordinals"].append(row.ordinal)
            item["source_relationships"].append(str(event.get("event_type") or "unknown"))
            for key in ("audio_seconds", "duration_seconds", "duration_sec"):
                if item["duration_seconds"] is None and isinstance(event.get(key), (int, float)):
                    item["duration_seconds"] = event[key]
    for item in by_id.values():
        path = Path(audio_root) / run_id / f"{item['utterance_id']}.wav"
        item["available"] = path.is_file() and not path.is_symlink()
        if item["available"] and item["duration_seconds"] is None:
            item["duration_seconds"] = _audio_duration(path)
        item["source_event_ordinals"] = sorted(set(item["source_event_ordinals"]))
        item["source_relationships"] = sorted(set(item["source_relationships"]))
        item["original_wav_path"], _ = sanitize_value(
            item["original_wav_path"], project_root=project_root
        )
    return sorted(by_id.values(), key=lambda item: item["utterance_id"])


def _subtitle_rows(rows: list[SourceEvent]) -> list[dict[str, Any]]:
    output = []
    for row in rows:
        event = row.event
        if event.get("event_type") != "translation" or event.get("subtitle_emitted") is False:
            continue
        target = event.get("target_text", event.get("translation", event.get("translated_text", "")))
        if not target:
            continue
        attempts = event.get("attempts") if isinstance(event.get("attempts"), list) else []
        output.append(
            {
                "timestamp": event.get("created_at", ""),
                "source_ko": event.get("source_text", event.get("text", "")),
                "final_zh_tw": target,
                "provider": event.get("route_id") or event.get("engine") or event.get("model") or "",
                "effective_profile": event.get("effective_profile_id") or event.get("profile_id") or "",
                "sentence_id": event.get("sentence_id", ""),
                "provisional_id": event.get("provisional_id") or event.get("subtitle_id") or "",
                "revision": event.get("provisional_final_revision", event.get("revision", "")),
                "translation_id": event.get("translation_id") or event.get("request_id") or "",
                "sequence_id": event.get("sequence_id", ""),
                "utterance_ids": ",".join(_event_ids(event)),
                "profile_generation": event.get("profile_generation", ""),
                "run_id": event.get("run_id", ""),
                "source_event_ordinal": row.ordinal,
                "source_event_file": row.source_file.name,
                "source_event_line": row.source_line,
                "provider_attempt_count": len(attempts),
            }
        )
    return output


def _write_event_parts(events: list[dict[str, Any]], output_dir: Path, max_part_bytes: int) -> list[str]:
    if max_part_bytes < 1:
        raise ValueError("max_part_bytes must be positive")
    lines = [(_json_dump(event) + "\n").encode("utf-8") for event in events]
    groups: list[list[bytes]] = []
    current: list[bytes] = []
    current_size = 0
    for line in lines:
        if current and current_size + len(line) > max_part_bytes:
            groups.append(current)
            current, current_size = [], 0
        current.append(line)
        current_size += len(line)
    if current:
        groups.append(current)
    names = (
        ["runtime_events.jsonl"]
        if len(groups) == 1
        else [f"runtime_events.part{index:03d}.jsonl" for index in range(1, len(groups) + 1)]
    )
    for name, group in zip(names, groups):
        (output_dir / name).write_bytes(b"".join(group))
    return names


def _safe_run_component(run_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", run_id).strip("._")
    if not safe:
        raise ValueError("run_id has no safe path characters")
    return safe[:120]


def _unique_destination(output_root: Path, run_id: str) -> Path:
    base = output_root / f"chatgpt_bundle_{_safe_run_component(run_id)}"
    if not base.exists():
        return base
    index = 2
    while (candidate := output_root / f"{base.name}_{index:02d}").exists():
        index += 1
    return candidate


def _profile_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    keys = ("source_profile_id", "content_profile_id", "effective_profile_id", "profile_id")
    transition_events = [
        event for event in events
        if event.get("event_type") in {"profile_resolution", "profile_control_reload", "profile_registry_reload"}
        or event.get("action") in {"profile_switched", "profile_activated"}
    ]
    states: list[tuple[str, Any]] = []
    for event in transition_events or events:
        profile_id = str(event.get("effective_profile_id") or event.get("profile_id") or "")
        generation = event.get("profile_generation")
        if not profile_id and generation is None:
            continue
        state = (profile_id, generation)
        if not states or states[-1] != state:
            states.append(state)
    return {
        key: sorted({str(event.get(key)) for event in events if event.get(key)})
        for key in keys
    } | {
        "generations": sorted({event.get("profile_generation") for event in events if isinstance(event.get("profile_generation"), int)}),
        "observed_state_sequence": [
            {"effective_profile_id": profile_id, "profile_generation": generation}
            for profile_id, generation in states
        ],
        "switch_count": max(0, len(states) - 1),
        "resolver_observation_count": sum(1 for event in events if event.get("event_type") == "profile_resolution"),
    }


def _manifest(
    rows: list[SourceEvent], *, run_id: str, event_files: list[str], subtitles: list[dict[str, Any]],
    audio: list[dict[str, Any]], redaction_count: int, config_available: bool, snapshot_cutoff: str,
) -> dict[str, Any]:
    events = [row.event for row in rows]
    timestamps = [_parse_timestamp(event.get("created_at")) for event in events]
    timestamps = [value for value in timestamps if value is not None]
    start = min(timestamps).isoformat() if timestamps else ""
    end = max(timestamps).isoformat() if timestamps else ""
    duration = (max(timestamps) - min(timestamps)).total_seconds() if len(timestamps) >= 2 else 0.0
    event_counts = Counter(str(event.get("event_type") or "unknown") for event in events)
    stt_providers = sorted({str(event.get("provider") or event.get("engine") or event.get("model")) for event in events if event.get("event_type") == "stt" and (event.get("provider") or event.get("engine") or event.get("model"))})
    translation_providers = sorted({str(event.get("route_id") or event.get("engine") or event.get("model")) for event in events if event.get("event_type") == "translation" and (event.get("route_id") or event.get("engine") or event.get("model"))})
    provisional = sum(
        1 for event in events
        if "provisional" in str(event.get("event_type") or "").lower()
        or event.get("phase") == "provisional"
        or event.get("action") == "provisional_displayed"
    )
    fallback = sum(
        1 for event in events
        if "fallback" in str(event.get("event_type") or "").lower()
        or (isinstance(event.get("attempts"), list) and len(event["attempts"]) > 1)
    )
    rejection = sum(
        1
        for event in events
        for value in [
            str(event.get("status") or event.get("action") or event.get("reason") or ""),
            *(str(attempt.get("status") or attempt.get("reason") or "") for attempt in event.get("attempts", []) if isinstance(attempt, dict)),
        ]
        if "reject" in value.lower()
    )
    errors = sum(1 for event in events if str(event.get("status") or "").lower() in {"error", "failed", "rejected"} or event.get("error_type"))
    return {
        "bundle_schema_version": BUNDLE_SCHEMA_VERSION,
        "run_id": run_id,
        "run_kind": str(events[0].get("run_kind") or "legacy-live") if events else "",
        "runtime": {
            "observed_event_start": start,
            "observed_event_end": end,
            "observed_event_duration_seconds": duration,
            "run_complete": _has_terminal_event(events),
            "snapshot_cutoff": snapshot_cutoff,
            "completion_note": "run_complete is true only when a persisted terminal lifecycle event exists",
        },
        "event_count": len(events),
        "event_counts": dict(sorted(event_counts.items())),
        "runtime_event_files": event_files,
        "profiles": _profile_summary(events),
        "stt_providers": stt_providers,
        "translation_providers": translation_providers,
        "subtitle_count": len(subtitles),
        "provisional_count": provisional,
        "fallback_count": fallback,
        "rejection_count": rejection,
        "error_warning_count": errors,
        "audio": {
            "indexed": len(audio),
            "available": sum(1 for item in audio if item["available"]),
            "included": sum(1 for item in audio if item["bundle_path"]),
        },
        "sanitization": {
            "redaction_count": redaction_count,
            "screenshots_included": False,
            "raw_http_headers_included": False,
        },
        "config": {
            "file": "config_sanitized.json" if config_available else None,
            "basis": "latest_unbound_dashboard_snapshot; not guaranteed to equal the historical run config",
        },
        "derived_files": ["CHATGPT_PROJECT_README.md", "manifest.json", "subtitles.tsv", "audio_index.json"],
        "event_source_provenance": [
            {"ordinal": row.ordinal, "source_file": row.source_file.name, "source_line": row.source_line}
            for row in rows
        ],
        "analysis_order": ["CHATGPT_PROJECT_README.md", "manifest.json", *event_files, "subtitles.tsv", "audio_index.json"],
    }


def _markdown(manifest: dict[str, Any], subtitles: list[dict[str, Any]]) -> str:
    runtime = manifest["runtime"]
    profiles = manifest["profiles"]
    lines = [
        _INSTRUCTION, "", f"# ChatGPT Project runtime bundle: `{manifest['run_id']}`", "",
        "## Run index", "",
        f"- Observed first persisted event: `{runtime['observed_event_start'] or 'unavailable'}`",
        f"- Observed last persisted event/snapshot cutoff: `{runtime['observed_event_end'] or runtime['snapshot_cutoff']}`",
        f"- Observed event-span duration: `{runtime['observed_event_duration_seconds']}` seconds",
        f"- Persisted terminal event: `{runtime['run_complete']}`",
        f"- Events: `{manifest['event_count']}`",
        f"- Subtitles: `{manifest['subtitle_count']}`; provisional observations: `{manifest['provisional_count']}`",
        f"- Fallbacks: `{manifest['fallback_count']}`; rejections: `{manifest['rejection_count']}`; errors/warnings: `{manifest['error_warning_count']}`",
        f"- Source/content/effective/profile IDs: `{profiles}`",
        f"- Profile generations: `{profiles['generations']}`; observed state transitions: `{profiles['switch_count']}`; resolver observations: `{profiles['resolver_observation_count']}`",
        f"- STT providers: `{manifest['stt_providers']}`",
        f"- Translation providers: `{manifest['translation_providers']}`", "",
        "## Files and analysis order", "",
    ]
    lines.extend(f"{index}. `{name}`" for index, name in enumerate(manifest["analysis_order"], start=1))
    lines += [
        "", "The JSONL parts preserve every selected-run event after secret/privacy sanitization and are ordered by source filename and line. Derived tables retain source event ordinals and identifiers.",
        "", "## Event-type glossary", "",
    ]
    glossary = {
        "audio": "capture/VAD evidence", "stt": "speech-to-text request/result and provider attribution",
        "sentence": "sentence-buffer/splitter decision", "translation": "translation request/result/publication evidence",
        "translation_fallback": "translation fallback decision", "subtitle": "subtitle display lifecycle",
        "profile_resolution": "scene/profile resolver observation or activation",
    }
    for event_type, count in manifest["event_counts"].items():
        lines.append(f"- `{event_type}` ({count}): {glossary.get(event_type, 'retained runtime event; inspect raw fields without assuming undocumented semantics')}")
    lines += ["", "## Chronological published subtitles", ""]
    headers = ["timestamp", "source Korean", "final zh-TW", "provider", "effective profile", "sentence/provisional/final IDs", "raw event"]
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("| " + " | ".join("---" for _ in headers) + " |")
    for row in subtitles:
        clean = lambda value: str(value).replace("|", "\\|").replace("\n", "<br>")
        identifiers = f"sentence={row['sentence_id']}; provisional={row['provisional_id']}; revision={row['revision']}; sequence={row['sequence_id']}"
        lines.append("| " + " | ".join(clean(value) for value in [row["timestamp"], row["source_ko"], row["final_zh_tw"], row["provider"], row["effective_profile"], identifiers, row["source_event_ordinal"]]) + " |")
    if not subtitles:
        lines.append("| — | — | — | — | — | — | — |")
    lines += ["", "## Evidence limitations", "", "- Fields absent from runtime logs are unavailable and are not fabricated.", "- STT text is not audio ground truth.", "- The config file is a sanitized current dashboard export unless the runtime event itself persisted a run-specific setting.", "- Screenshots and scene captures are intentionally excluded.", ""]
    return "\n".join(lines)


def export_bundle(
    *, run_id: str, log_dir: Path, output_root: Path, project_root: Path,
    config_path: Path | None = None, audio_root: Path | None = None,
    include_audio: bool = False, max_part_bytes: int = DEFAULT_MAX_PART_BYTES,
) -> dict[str, Any]:
    event_files = discover_event_files(log_dir)
    rows = read_run_events(event_files, run_id)
    if not rows:
        raise ValueError(f"run_id not found: {run_id}")
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    destination = _unique_destination(output_root, run_id)
    temp_dir = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=output_root))
    redactions = 0
    try:
        sanitized_events = []
        for row in rows:
            clean, count = sanitize_value(row.event, project_root=project_root)
            sanitized_events.append(clean)
            redactions += count
        part_names = _write_event_parts(sanitized_events, temp_dir, max_part_bytes)
        subtitles = _subtitle_rows(rows)
        clean_subtitles = []
        for subtitle in subtitles:
            clean, count = sanitize_value(subtitle, project_root=project_root)
            clean_subtitles.append(clean)
            redactions += count
        with (temp_dir / "subtitles.tsv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(clean_subtitles[0].keys()) if clean_subtitles else ["run_id"], dialect="excel-tab")
            writer.writeheader()
            writer.writerows(clean_subtitles)
        resolved_audio_root = Path(audio_root or Path(log_dir) / "audio_dump")
        audio = _audio_index(rows, audio_root=resolved_audio_root, run_id=run_id, project_root=project_root)
        if include_audio:
            controlled = (resolved_audio_root / run_id).resolve()
            for item in audio:
                source = resolved_audio_root / run_id / f"{item['utterance_id']}.wav"
                if not item["available"] or source.is_symlink() or source.resolve().parent != controlled:
                    continue
                relative = Path("audio") / source.name
                (temp_dir / "audio").mkdir(exist_ok=True)
                shutil.copy2(source, temp_dir / relative)
                item["bundle_path"] = relative.as_posix()
        (temp_dir / "audio_index.json").write_text(_json_dump(audio, pretty=True) + "\n", encoding="utf-8")
        config_available = bool(config_path and Path(config_path).is_file())
        if config_available:
            try:
                config = json.loads(Path(config_path).read_text(encoding="utf-8-sig"))
            except (json.JSONDecodeError, ValueError):
                config = {"unavailable_reason": "invalid_config_json"}
            config, count = sanitize_value(config, project_root=project_root)
            redactions += count
            (temp_dir / "config_sanitized.json").write_text(_json_dump(config, pretty=True) + "\n", encoding="utf-8")
        snapshot_cutoff = datetime.now(timezone.utc).isoformat()
        manifest = _manifest(rows, run_id=run_id, event_files=part_names, subtitles=clean_subtitles, audio=audio, redaction_count=redactions, config_available=config_available, snapshot_cutoff=snapshot_cutoff)
        manifest["integrity"] = {
            name: {"sha256": hashlib.sha256((temp_dir / name).read_bytes()).hexdigest(), "size_bytes": (temp_dir / name).stat().st_size}
            for name in [
                *part_names,
                "subtitles.tsv",
                "audio_index.json",
                *(["config_sanitized.json"] if config_available else []),
                *(item["bundle_path"] for item in audio if item["bundle_path"]),
            ]
        }
        (temp_dir / "manifest.json").write_text(_json_dump(manifest, pretty=True) + "\n", encoding="utf-8")
        (temp_dir / "CHATGPT_PROJECT_README.md").write_text(_markdown(manifest, clean_subtitles), encoding="utf-8")
        temp_dir.rename(destination)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    files = [path for path in destination.rglob("*") if path.is_file()]
    return {
        "run_id": run_id,
        "output_path": str(destination.resolve()),
        "file_count": len(files),
        "total_bytes": sum(path.stat().st_size for path in files),
        "event_count": len(rows),
        "runtime_event_files": part_names,
        "audio_included": sum(1 for item in audio if item["bundle_path"]),
    }


def bundle_event_paths(path: Path) -> list[Path]:
    path = Path(path)
    if path.is_file():
        return [path]
    manifest_path = path / "manifest.json"
    if not manifest_path.is_file():
        return []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    root = path.resolve()
    result = []
    for name in manifest.get("runtime_event_files", []):
        if not re.fullmatch(r"runtime_events(?:\.part\d{3})?\.jsonl", str(name)):
            continue
        candidate = (path / name).resolve()
        if candidate.parent == root and candidate.is_file():
            result.append(candidate)
    return result
