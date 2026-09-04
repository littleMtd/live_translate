import csv
import json
import wave
from pathlib import Path

from scripts.analyze_runtime_events import analyze_runtime_events
from scripts.llm_quality_reviewer import iter_translation_events, resolve_event_paths
from utils.chatgpt_bundle import bundle_event_paths, export_bundle, list_runs, sanitize_value


def _write_events(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events), encoding="utf-8")


def _event(event_type: str, run_id: str = "run-a", second: int = 0, **extra):
    return {
        "schema_version": 5,
        "event_type": event_type,
        "run_id": run_id,
        "run_kind": "live",
        "created_at": f"2026-09-03T00:00:{second:02d}+00:00",
        **extra,
    }


def _export(tmp_path: Path, *, include_audio=False, max_part_bytes=1024 * 1024):
    project = tmp_path / "project"
    logs = project / "logs"
    first = [
        _event("stt", utterance_id="utt-1", provider="elevenlabs", confidence=0.8),
        _event(
            "translation", second=1, sequence_id=7, source_text="안녕", target_text="你好",
            source_utterance_ids=["utt-1"], route_id="deepseek:deepseek-v4-flash",
            profile_id="hades_chxxnnx", effective_profile_id="hades_chxxnnx",
            profile_generation=2, subtitle_emitted=True, provisional_id="p-1",
            attempts=[{"engine": "deepseek", "raw_output": "你好"}],
        ),
    ]
    second = [
        _event("profile_resolution", second=2, source_profile_id="isegye_lilpa", content_profile_id="hades_chxxnnx", effective_profile_id="hades_chxxnnx", profile_generation=2),
        _event("stt", run_id="run-b", utterance_id="other", authorization="Bearer hidden"),
    ]
    _write_events(logs / "runtime_events_20260903.jsonl", first)
    _write_events(logs / "runtime_events_20260904.jsonl", second)
    config = {"translation": {"api_key": "secret", "token_prompt": 22}, "nested": [{"Authorization": "Bearer abc"}], "provider_headers": {"X-Trace": "private"}}
    (logs / "live_translate_config.json").write_text(json.dumps(config), encoding="utf-8")
    audio_dir = logs / "audio_dump" / "run-a"
    audio_dir.mkdir(parents=True)
    with wave.open(str(audio_dir / "utt-1.wav"), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\0\0" * 800)
    (logs / "screenshot.png").write_bytes(b"not exported")
    result = export_bundle(
        run_id="run-a", log_dir=logs, output_root=project / "exports", project_root=project,
        config_path=logs / "live_translate_config.json", audio_root=logs / "audio_dump",
        include_audio=include_audio, max_part_bytes=max_part_bytes,
    )
    return project, Path(result["output_path"]), result, first + second[:1]


def test_selected_run_complete_preservation_order_and_provenance(tmp_path):
    _, bundle, result, expected = _export(tmp_path)
    exported = [json.loads(line) for path in bundle_event_paths(bundle) for line in path.read_text(encoding="utf-8").splitlines()]
    assert exported == expected
    assert result["event_count"] == 3
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    assert [row["source_line"] for row in manifest["event_source_provenance"]] == [1, 2, 1]
    assert manifest["profiles"]["generations"] == [2]
    assert manifest["profiles"]["switch_count"] == 0
    assert manifest["profiles"]["resolver_observation_count"] == 1
    assert manifest["translation_providers"] == ["deepseek:deepseek-v4-flash"]
    rows = list(csv.DictReader((bundle / "subtitles.tsv").open(encoding="utf-8"), dialect="excel-tab"))
    assert rows[0]["run_id"] == "run-a"
    assert rows[0]["source_event_ordinal"] == "2"
    assert rows[0]["profile_generation"] == "2"


def test_recursive_secret_redaction_does_not_remove_token_telemetry(tmp_path):
    _, bundle, _, _ = _export(tmp_path)
    config = json.loads((bundle / "config_sanitized.json").read_text(encoding="utf-8"))
    assert config["translation"] == {"api_key": "<REDACTED>", "token_prompt": 22}
    assert config["nested"][0]["Authorization"] == "<REDACTED>"
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    assert config["provider_headers"] == "<REDACTED>"
    assert manifest["sanitization"]["redaction_count"] == 3
    contents = "".join(path.read_text(encoding="utf-8", errors="ignore") for path in bundle.rglob("*") if path.is_file())
    assert '\"api_key\": \"secret\"' not in contents


def test_runtime_nested_headers_tokens_and_screenshot_payload_are_sanitized(tmp_path):
    clean, count = sanitize_value(
        {
            "attempt": {"request_headers": {"Authorization": "Bearer abc"}},
            "message": "credential sk-abcdefghijklmnop",
            "screenshot_base64": "raw-image",
            "token_output": 17,
            "openrouter_api_key": "sk-too-short-but-secret",
            "httpHeaders": {"X-Api-Key": "plain-secret"},
            "note": "Authorization: Basic dXNlcjpwYXNz",
            "aws_credential": "plain-secret",
            "aws_secret_access_key": "plain-secret-2",
            "github_token": "plain-secret-3",
        },
        project_root=tmp_path,
    )
    assert clean["attempt"]["request_headers"] == "<REDACTED>"
    assert clean["message"] == "credential <REDACTED>"
    assert clean["screenshot_base64"] == "<OMITTED_PRIVACY_MEDIA>"
    assert clean["token_output"] == 17
    assert clean["openrouter_api_key"] == "<REDACTED>"
    assert clean["httpHeaders"] == "<REDACTED>"
    assert clean["note"] == "Authorization: Basic <REDACTED>"
    assert clean["aws_credential"] == "<REDACTED>"
    assert clean["aws_secret_access_key"] == "<REDACTED>"
    assert clean["github_token"] == "<REDACTED>"
    assert count == 9


def test_audio_is_indexed_by_default_and_optionally_copied(tmp_path):
    _, bundle, result, _ = _export(tmp_path)
    index = json.loads((bundle / "audio_index.json").read_text(encoding="utf-8"))
    assert result["audio_included"] == 0
    assert index[0]["available"] is True
    assert index[0]["bundle_path"] is None
    assert "<PROJECT_ROOT>" in index[0]["original_wav_path"]
    assert not (bundle / "audio").exists()

    _, with_audio, result, _ = _export(tmp_path / "second", include_audio=True)
    assert result["audio_included"] == 1
    assert (with_audio / "audio" / "utt-1.wav").is_file()


def test_missing_wav_and_screenshot_are_not_exported(tmp_path):
    _, bundle, _, _ = _export(tmp_path)
    index = json.loads((bundle / "audio_index.json").read_text(encoding="utf-8"))
    assert all(item["utterance_id"] != "other" for item in index)
    assert not any("screenshot" in path.name.lower() for path in bundle.rglob("*"))


def test_large_log_splits_deterministically_without_truncation(tmp_path):
    _, bundle, result, expected = _export(tmp_path, max_part_bytes=250)
    assert result["runtime_event_files"] == ["runtime_events.part001.jsonl", "runtime_events.part002.jsonl", "runtime_events.part003.jsonl"]
    exported = [json.loads(line) for path in bundle_event_paths(bundle) for line in path.read_text(encoding="utf-8").splitlines()]
    assert exported == expected


def test_bundle_can_be_read_without_original_logs_by_analyzer_and_harness(tmp_path):
    project, bundle, _, _ = _export(tmp_path)
    for path in (project / "logs").glob("runtime_events_*.jsonl"):
        path.unlink()
    report = analyze_runtime_events(bundle, run_id="run-a")
    assert report["available"] is True
    assert report["total_events"] == 3
    paths = resolve_event_paths([str(bundle)])
    translations = list(iter_translation_events(paths, run_ids={"run-a"}))
    assert len(translations) == 1
    assert translations[0]["target_text"] == "你好"


def test_list_runs_filters_malformed_lines_and_reports_observed_bounds(tmp_path):
    logs = tmp_path / "logs"
    _write_events(logs / "runtime_events_20260903.jsonl", [_event("stt"), _event("translation", run_id="run-b", second=2)])
    with (logs / "runtime_events_20260903.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("not-json\n")
    runs = list_runs(logs)
    assert [row["run_id"] for row in runs] == ["run-b", "run-a"]
    assert runs[0]["event_count"] == 1
    assert runs[0]["run_complete"] is False
