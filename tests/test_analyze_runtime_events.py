import json

from scripts.analyze_runtime_events import analyze_runtime_events


def _write_jsonl(path, rows):
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_analyze_runtime_events_returns_unavailable_for_missing_file(tmp_path):
    report = analyze_runtime_events(tmp_path / "missing.jsonl")

    assert report["available"] is False


def test_analyze_runtime_events_summarizes_translation_events(tmp_path):
    path = tmp_path / "runtime_events_20260514.jsonl"
    _write_jsonl(
        path,
        [
            {
                "event_type": "translation",
                "run_id": "run-a",
                "created_at": "2026-05-14T00:00:00+00:00",
                "status": "success",
                "result_source": "api",
                "cache_status": "miss",
                "engine": "nvidia",
                "latency_ms": 100,
                "subtitle_emitted": True,
                "quality_flags": [],
                "source_text": "안녕하세요",
                "target_text": "你好",
            },
            {
                "event_type": "translation",
                "run_id": "run-a",
                "created_at": "2026-05-14T00:00:01+00:00",
                "status": "filtered",
                "result_source": "policy",
                "cache_status": "skipped",
                "engine": "",
                "latency_ms": 10,
                "subtitle_emitted": False,
                "quality_flags": ["empty_target"],
                "source_text": "... ...",
                "target_text": "",
            },
        ],
    )

    report = analyze_runtime_events(path, top_n=1)

    assert report["available"] is True
    assert report["translation_events"] == 2
    assert report["run_ids"] == ["run-a"]
    assert report["by_status"][0] == {"value": "success", "count": 1}
    assert {"value": "True", "count": 1} in report["by_subtitle_emitted"]
    assert {"flag": "empty_target", "count": 1} in report["quality_flags"]
    assert report["latency_ms"]["avg"] == 55
    assert len(report["latest"]) == 1
    assert len(report["flagged_samples"]) == 1
