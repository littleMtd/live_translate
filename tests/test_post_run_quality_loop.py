from pathlib import Path
import json

from scripts import post_run_quality_loop as loop
from scripts.analyze_runtime_events import analyze_runtime_events


def test_post_run_quality_loop_runs_existing_tools(tmp_path, monkeypatch):
    captured_json: list[tuple[list[str], object]] = []
    captured_run: list[list[str]] = []
    event_a = tmp_path / "runtime_events_a.jsonl"
    event_b = tmp_path / "runtime_events_b.jsonl"
    event_a.write_text('{"event_type":"translation","run_id":"a"}\n', encoding="utf-8")
    event_b.write_text('{"event_type":"translation","run_id":"b"}\n', encoding="utf-8")

    def fake_run_capture_json(command, output):
        captured_json.append((command, output))
        output.write_text("{}", encoding="utf-8")
        return 0

    def fake_run(command):
        captured_run.append(command)
        return 0

    monkeypatch.setattr(loop.sys, "executable", "py")
    monkeypatch.setattr(loop, "_run_capture_json", fake_run_capture_json)
    monkeypatch.setattr(loop, "_run", fake_run)

    result = loop.main([
        "--events",
        str(event_a),
        str(event_b),
        "--run-id",
        "run-1",
        "--output-dir",
        str(tmp_path),
        "--snapshot",
        "data/replay_eval_snapshot.jsonl",
    ])

    assert result == 0
    assert len(captured_json) == 1
    assert captured_json[0][0][:3] == [
        "py",
        "scripts/analyze_runtime_events.py",
        "--events",
    ]
    combined = captured_json[0][0][3]
    assert captured_json[0][0][4:] == ["--run-kind", "live", "--json"]
    assert combined.endswith("runtime_events_combined.jsonl")
    assert "run_id\":\"a" in Path(combined).read_text(encoding="utf-8")
    assert "run_id\":\"b" in Path(combined).read_text(encoding="utf-8")
    assert captured_json[0][1].name == "runtime_report.json"
    assert captured_run[0][:4] == [
        "py",
        "scripts/suggest_corrections.py",
        "--events",
        combined,
    ]
    assert "--run-id" in captured_run[0]
    assert "run-1" in captured_run[0]
    assert "--json-output" in captured_run[0]
    json_output = captured_run[0][captured_run[0].index("--json-output") + 1]
    assert json_output.endswith("glossary_candidates.json")
    assert captured_run[1] == [
        "py",
        "scripts/replay_eval.py",
        "run",
        "--snapshot",
        "data/replay_eval_snapshot.jsonl",
        "--update",
    ]


def test_post_run_explicit_cafe_kind_reaches_analyzer(tmp_path, monkeypatch):
    events = tmp_path / "runtime_events_20261008.jsonl"
    events.write_text(
        '{"event_type":"translation","run_id":"live","source_text":"one"}\n'
        '{"event_type":"translation","run_id":"cafe","run_kind":"cafe_clip","source_text":"two"}\n',
        encoding="utf-8",
    )

    def capture(command, output):
        kind = command[command.index("--run-kind") + 1]
        path = Path(command[command.index("--events") + 1])
        output.write_text(json.dumps(analyze_runtime_events(path, run_kind=kind)), encoding="utf-8")
        return 0

    monkeypatch.setattr(loop, "_run_capture_json", capture)
    monkeypatch.setattr(loop, "_run", lambda command: 0)
    assert loop.main(["--events", str(events), "--run-kind", "cafe_clip",
                      "--output-dir", str(tmp_path), "--skip-replay-update"]) == 0
    report_path = next(tmp_path.glob("*/runtime_report.json"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["run_kind_filter"] == "cafe_clip"
    assert report["translation_events"] == 1
