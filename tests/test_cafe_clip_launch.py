import json
import sys
from types import SimpleNamespace

import pytest
import main

from scripts.analyze_runtime_events import analyze_runtime_events, parse_args
from scripts.collection_sanity_report import build_collection_sanity_report
from scripts.post_run_quality_loop import _prepare_event_input
from scripts.replay_eval import iter_translation_events
from scripts.sample_labeling_cases import read_runtime_rows, translation_population
from utils import config_export
from utils.chatgpt_bundle import list_runs
from utils.runtime_events import RuntimeEventWriter
from utils import runtime_events as runtime_event_module
from utils import chatgpt_bundle


@pytest.mark.parametrize("extra", [[], ["--stt-only"], ["--listen"]])
def test_profile_cli_configures_snapshot_without_mutating_cfg(monkeypatch, tmp_path, extra):
    from modules.profile_context import ProfileState
    state = ProfileState(main.profile_state.registry, source_profile_id="isegye_lilpa")
    monkeypatch.setattr(main, "profile_state", state)
    before = config_export._to_dict()
    assert main.cfg.active_streamer_profile != "url"
    args = main._parse_args(["--profile", "url", *extra])
    main._configure_profile(args)
    snapshot = state.current()
    assert snapshot.source_profile_id == snapshot.effective_profile_id == "url"
    assert snapshot.mode == "manual"
    assert snapshot.translation_profile_applied is True
    assert snapshot.stt_glossary_applied == main.cfg.stt.use_profile_glossary
    assert config_export._to_dict() == before
    target = tmp_path / "config.json"
    monkeypatch.setattr(config_export, "_EXPORT_PATH", target)
    config_export.write()
    assert json.loads(target.read_text(encoding="utf-8")) == before


def test_profile_cli_rejects_invalid_combinations():
    for extra in (["--show-identity-roi"], ["--calibrate-identity-roi"]):
        with pytest.raises(SystemExit):
            main._parse_args(["--profile", "url", *extra])
    with pytest.raises(SystemExit):
        main._parse_args(["--profile", "not_a_profile"])


def test_mixed_daily_events_are_selected_by_run_kind(tmp_path):
    path = tmp_path / "runtime_events_20261008.jsonl"
    events = [
        {"schema_version": 2, "event_type": "translation", "run_id": "live-run",
         "source_text": "live source", "target_text": "live target", "engine": "groq"},
        {"schema_version": 2, "event_type": "translation", "run_id": "cafe-run",
         "run_kind": "cafe_clip", "source_text": "cafe source", "target_text": "cafe target", "engine": "groq"},
    ]
    path.write_text("".join(json.dumps(event) + "\n" for event in events) + "{broken", encoding="utf-8")
    report = analyze_runtime_events(path)
    assert len(report["by_run_kind"]) == 2
    assert report["translation_events"] == report["total_events"] == 1
    assert report["run_ids"] == ["live-run"]
    assert analyze_runtime_events(path, run_kind="cafe_clip")["run_ids"] == ["cafe-run"]
    assert parse_args(["--run-kind", "cafe_clip"]).run_kind == "cafe_clip"
    assert [e["run_id"] for e in iter_translation_events([str(path)])] == ["live-run"]
    assert [e["run_id"] for e in iter_translation_events([str(path)], "cafe_clip")] == ["cafe-run"]
    rows = read_runtime_rows(path)
    assert [r["event"]["run_id"] for r in translation_population(rows)] == ["live-run"]
    assert [r["event"]["run_id"] for r in translation_population(rows, run_kind="cafe_clip")] == ["cafe-run"]
    live_report = build_collection_sanity_report(events_path=path, audio_root=tmp_path, min_population=0)
    cafe_report = build_collection_sanity_report(events_path=path, audio_root=tmp_path,
                                                 min_population=0, run_kind="cafe_clip")
    assert [item["value"] for item in live_report["profiles"]] == ["unknown"]
    assert live_report["counts"]["translation_events"] == cafe_report["counts"]["translation_events"] == 1
    combined = _prepare_event_input([str(path)], tmp_path)
    assert [json.loads(line)["run_id"] for line in combined.read_text(encoding="utf-8").splitlines()] == ["live-run"]
    assert {run["run_kind"] for run in list_runs(tmp_path)} == {"legacy-live", "cafe_clip"}


def test_cafe_clip_writer_and_atomic_config_export(tmp_path, monkeypatch):
    writer = RuntimeEventWriter(log_dir=tmp_path, run_id="cafe-run", run_kind="cafe_clip")
    writer.emit("runtime_lifecycle", action="started")
    assert list_runs(tmp_path)[0]["run_kind"] == "cafe_clip"
    target = tmp_path / "config.json"
    target.write_text("old", encoding="utf-8")
    monkeypatch.setattr(config_export, "_EXPORT_PATH", target)
    def fail_replace(source, destination):
        assert target.read_text(encoding="utf-8") == "old"
        raise OSError("replace failed")
    monkeypatch.setattr(config_export.os, "replace", fail_replace)
    with pytest.raises(OSError):
        config_export.write()
    assert target.read_text(encoding="utf-8") == "old"
    assert list(tmp_path.glob("*.tmp")) == []


def test_cafe_clip_shutdown_skips_automatic_bundle(monkeypatch):
    emitted = []
    writer = SimpleNamespace(run_kind="cafe_clip", run_id="cafe-run",
                             emit=lambda *args, **kwargs: emitted.append((args, kwargs)))
    monkeypatch.setattr(runtime_event_module, "runtime_events", writer)
    monkeypatch.setattr(chatgpt_bundle, "export_bundle",
                        lambda **kwargs: pytest.fail("cafe clip must not auto-export"))
    assert main._export_chatgpt_bundle_on_shutdown(status="stopped") is None
    assert emitted[0][1]["action"] == "shutdown"


def test_writer_terminates_partial_line_left_by_forced_stop(tmp_path):
    clock = lambda: "2026-10-08T12:00:00+00:00"
    killed = RuntimeEventWriter(log_dir=tmp_path, run_id="killed", run_kind="cafe_clip", clock=clock)
    killed.emit("before_kill", n=1)
    path = killed.path
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"event_type":"half')  # Forced stop mid-write.

    nxt = RuntimeEventWriter(log_dir=tmp_path, run_id="next", run_kind="live", clock=clock)
    nxt.emit("run_start", n=2)
    nxt.emit("after", n=3)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[1] == '{"event_type":"half'
    parsed = [json.loads(line) for line in lines if line.startswith("{") and line.endswith("}")]
    assert [row["run_id"] for row in parsed] == ["killed", "next", "next"]
    assert [row["event_type"] for row in parsed][1:] == ["run_start", "after"]


def test_writer_does_not_add_blank_lines_to_clean_files(tmp_path):
    clock = lambda: "2026-10-08T12:00:00+00:00"
    first = RuntimeEventWriter(log_dir=tmp_path, run_id="a", run_kind="live", clock=clock)
    first.emit("x")
    second = RuntimeEventWriter(log_dir=tmp_path, run_id="b", run_kind="live", clock=clock)
    second.emit("y")
    assert "" not in second.path.read_text(encoding="utf-8").splitlines()


def test_five_kinds_preserve_each_consumer_policy(tmp_path):
    events = [dict(schema_version=2, event_type="translation", run_id=kind,
                   run_kind=kind, source_text="source", target_text="target")
              for kind in ("live", "test", "replay", "benchmark", "cafe_clip")]
    events.append(dict(schema_version=2, event_type="translation", run_id="legacy",
                       source_text="source", target_text="target"))
    path = tmp_path / "runtime_events_20261008.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    expected = {"live", "legacy", "test", "replay", "benchmark"}
    report = analyze_runtime_events(path)
    assert report["run_ids"] == ["legacy", "live"]
    assert report["translation_events"] == 2
    assert analyze_runtime_events(path, run_kind="all")["translation_events"] == 6
    for kind in ("test", "replay", "benchmark", "cafe_clip"):
        assert analyze_runtime_events(path, run_kind=kind)["run_ids"] == [kind]
    assert {e["run_id"] for e in iter_translation_events([str(path)])} == expected
    assert {r["event"]["run_id"] for r in translation_population(read_runtime_rows(path))} == expected
    assert build_collection_sanity_report(events_path=path, audio_root=tmp_path,
                                          min_population=0)["counts"]["translation_events"] == 5
    combined = _prepare_event_input([str(path)], tmp_path)
    assert {json.loads(l)["run_id"] for l in combined.read_text(encoding="utf-8").splitlines()} == expected
    for kind, count in (("cafe_clip", 1), ("all", 6)):
        assert len(list(iter_translation_events([str(path)], kind))) == count
        assert len(translation_population(read_runtime_rows(path), run_kind=kind)) == count
        assert build_collection_sanity_report(events_path=path, audio_root=tmp_path,
                                              min_population=0, run_kind=kind)["counts"]["translation_events"] == count
        assert len(_prepare_event_input([str(path)], tmp_path, kind).read_text(encoding="utf-8").splitlines()) == count


def test_config_export_cleans_only_old_owned_temps(tmp_path, monkeypatch):
    import os
    import time
    target = tmp_path / "config.json"
    monkeypatch.setattr(config_export, "_EXPORT_PATH", target)
    stale = tmp_path / "config.json.abcdefgh.tmp"
    fresh = tmp_path / "config.json.12345678.tmp"
    other = tmp_path / "other.json.abcdefgh.tmp"
    unrelated = tmp_path / "config.json.user.tmp"
    active = tmp_path / "config.json.active01.tmp"
    old = time.time() - config_export._STALE_TEMP_SECONDS - 10
    for path in (stale, fresh, other, unrelated, active):
        path.write_text("leave unless stale and owned", encoding="utf-8")
        if path != fresh:
            os.utime(path, (old, old))
    with active.open("a", encoding="utf-8"):
        config_export.write()
        if sys.platform == "win32":
            # Windows refuses to delete a file another handle has open; cleanup
            # must swallow that and still export. POSIX unlinks open files, so
            # there is no refusal to exercise there.
            assert active.exists()
    assert not stale.exists()
    assert all(p.exists() for p in (fresh, other, unrelated))
    assert json.loads(target.read_text(encoding="utf-8")) == config_export._to_dict()


def test_writer_retries_newline_after_first_append_failure(tmp_path, monkeypatch):
    from pathlib import Path
    writer = RuntimeEventWriter(log_dir=tmp_path, run_id="retry",
                                clock=lambda: "2026-10-08T12:00:00+00:00")
    path = writer.path
    path.write_bytes(b'{"broken":')
    original = Path.open
    failed = False
    def fail_once(self, mode="r", *args, **kwargs):
        nonlocal failed
        if self == path and mode == "a" and not failed:
            failed = True
            raise OSError("first append failed")
        return original(self, mode, *args, **kwargs)
    monkeypatch.setattr(Path, "open", fail_once)
    writer.emit("first")
    assert path not in writer._terminated_paths
    writer.emit("retry")
    assert path in writer._terminated_paths
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[1])["event_type"] == "retry"


def test_truncated_utf8_record_does_not_hide_following_event(tmp_path):
    writer = RuntimeEventWriter(log_dir=tmp_path, run_id="valid", run_kind="live",
                                clock=lambda: "2026-10-08T12:00:00+00:00")
    path = writer.path
    # A forced stop in the middle of a three-byte Chinese character.
    path.write_bytes(b'{"source_text":"' + "中".encode("utf-8")[:2])
    writer.emit("translation", schema_version=2, source_text="正常", target_text="正常")
    assert analyze_runtime_events(path)["run_ids"] == ["valid"]
    from scripts.collection_sanity_report import read_runtime_rows as collection_rows
    for reader in (read_runtime_rows, collection_rows):
        rows = reader(path)
        assert len(rows) == 1
        assert rows[0]["line_no"] == 2
        assert rows[0]["event"]["run_id"] == "valid"
    assert build_collection_sanity_report(events_path=path, audio_root=tmp_path,
                                          min_population=0)["counts"]["translation_events"] == 1
    combined = _prepare_event_input([str(path)], tmp_path)
    assert [json.loads(l)["run_id"] for l in combined.read_text(encoding="utf-8").splitlines()] == ["valid"]


def test_requested_profile_drives_probe_and_stt_when_cfg_differs(monkeypatch):
    import threading
    from modules import translator, stt
    from modules.profile_context import ProfileState, bound_profile_snapshot, build_registry_stt_glossary
    from modules.translator import get_translation_profile
    state = ProfileState(main.profile_state.registry, source_profile_id=main.cfg.active_streamer_profile)
    for module in (main, translator, stt):
        monkeypatch.setattr(module, "profile_state", state)
    main._configure_profile(main._parse_args(["--profile", "url"]))
    snapshot = state.current()
    assert main.cfg.active_streamer_profile != snapshot.effective_profile_id
    engine = stt.STTEngine.__new__(stt.STTEngine)
    prompt = engine._build_groq_prompt()
    glossary = build_registry_stt_glossary(state.registry, "url")
    assert engine._last_groq_prompt_inputs["profile_id"] == "url"
    assert engine._last_groq_prompt_inputs["selected_glossary"] == glossary
    assert "url" != main.cfg.active_streamer_profile
    assert prompt
    shared = translator._new_translator_shared_state()
    shared.fallback.active_idx = 1
    stop = threading.Event()
    reached = threading.Event()
    seen = {}
    monkeypatch.setattr(translator, "_build_engine_chain", lambda: [])
    def probe(engines, fallback, text, system_prompt, *args, **kwargs):
        seen["snapshot"] = bound_profile_snapshot()
        seen["prompt"] = system_prompt
        reached.set()
        stop.set()
    monkeypatch.setattr(translator, "probe_primary_recovery", probe)
    thread = translator._start_fallback_probe_thread(shared, stop, interval_seconds=0.001)
    try:
        assert reached.wait(2)
    finally:
        stop.set()
        thread.join(timeout=2)
    assert not thread.is_alive()
    assert seen["snapshot"] == snapshot
    assert get_translation_profile("url") in seen["prompt"]
    assert get_translation_profile(main.cfg.active_streamer_profile) not in seen["prompt"]
