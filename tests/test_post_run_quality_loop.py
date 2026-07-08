from pathlib import Path

from scripts import post_run_quality_loop as loop


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
    assert captured_json[0][0][4:] == ["--json"]
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
    assert captured_run[1] == [
        "py",
        "scripts/replay_eval.py",
        "run",
        "--snapshot",
        "data/replay_eval_snapshot.jsonl",
        "--update",
    ]
