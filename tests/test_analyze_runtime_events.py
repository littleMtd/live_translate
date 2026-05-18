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


def _translation_event(**overrides):
    base = {
        "event_type": "translation",
        "run_id": "run-x",
        "created_at": "2026-05-14T00:00:00+00:00",
        "status": "success",
        "result_source": "api",
        "cache_status": "miss",
        "engine": "claude",
        "latency_ms": 100,
        "subtitle_emitted": True,
        "quality_flags": [],
        "source_text": "안녕",
        "target_text": "你好",
        "filter_reason": "",
    }
    base.update(overrides)
    return base


def test_empty_target_summary_groups_by_source_and_reason(tmp_path):
    path = tmp_path / "runtime_events_20260514.jsonl"
    _write_jsonl(
        path,
        [
            _translation_event(
                status="filtered",
                result_source="policy",
                filter_reason="stt_template_garbage",
                target_text="",
            ),
            _translation_event(
                status="success",
                result_source="api",
                filter_reason="",
                target_text="   ",
            ),
            _translation_event(
                status="success",
                result_source="api",
                target_text="正常翻譯",
            ),
        ],
    )

    report = analyze_runtime_events(path, top_n=1)

    assert report["empty_targets"]["total"] == 2
    assert report["empty_targets"]["by_status"] == [
        {"value": "filtered", "count": 1},
        {"value": "success", "count": 1},
    ]
    assert report["empty_targets"]["by_result_source"] == [
        {"value": "policy", "count": 1},
        {"value": "api", "count": 1},
    ]
    assert report["empty_targets"]["by_filter_reason"] == [
        {"value": "stt_template_garbage", "count": 1}
    ]
    assert report["empty_targets"]["samples"][0]["filter_reason"] == "stt_template_garbage"


def test_by_filter_reason_aggregation(tmp_path):
    """A2: filter_reason distribution surfaces too_long / too_short / etc."""
    path = tmp_path / "runtime_events_20260514.jsonl"
    _write_jsonl(
        path,
        [
            _translation_event(status="filtered", filter_reason="too_long",   target_text=""),
            _translation_event(status="filtered", filter_reason="too_long",   target_text=""),
            _translation_event(status="filtered", filter_reason="too_short",  target_text=""),
            _translation_event(status="filtered", filter_reason="duplicate",  target_text=""),
            _translation_event(status="success"),  # no filter_reason → excluded from this aggregation
        ],
    )

    report = analyze_runtime_events(path)

    reasons = {item["value"]: item["count"] for item in report["by_filter_reason"]}
    assert reasons == {"too_long": 2, "too_short": 1, "duplicate": 1}


def test_status_breakdown_separates_denominators(tmp_path):
    """A2: success / filtered / failed counted separately for ratio computation."""
    path = tmp_path / "runtime_events_20260514.jsonl"
    _write_jsonl(
        path,
        [
            _translation_event(status="success"),
            _translation_event(status="success"),
            _translation_event(status="filtered", filter_reason="too_long", target_text=""),
            _translation_event(status="failed", target_text=""),
        ],
    )

    report = analyze_runtime_events(path)
    breakdown = report["status_breakdown"]

    assert breakdown == {
        "total":    4,
        "success":  2,
        "filtered": 1,
        "failed":   1,
        "other":    0,
    }


def test_latency_percentiles_p50_p95_p99(tmp_path):
    """A2: percentile suite (p50/p95/p99) coexists with existing avg/max."""
    path = tmp_path / "runtime_events_20260514.jsonl"
    # 100 evenly spaced latencies 1..100; percentiles are well-defined.
    rows = [_translation_event(latency_ms=value) for value in range(1, 101)]
    _write_jsonl(path, rows)

    report = analyze_runtime_events(path)
    latency = report["latency_ms"]

    assert latency["count"] == 100
    assert latency["max"] == 100
    # nearest-rank percentiles: index = min(n-1, int(n*p))
    assert latency["p50"] == 51
    assert latency["p95"] == 96
    assert latency["p99"] == 100  # int(100*0.99)=99 → ordered[99] = 100
    # avg / max preserved alongside new keys
    assert "avg" in latency and "max" in latency


def test_run_summaries_group_by_run_id_with_labels(tmp_path):
    path = tmp_path / "runtime_events_20260514.jsonl"
    labels_path = tmp_path / "run_labels.json"
    labels_path.write_text(
        json.dumps(
            {
                "run-a": {
                    "label": "commute_radio_control",
                    "note": "same streamer, low-confusion control",
                },
                "run-b": "singing_stress",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _write_jsonl(
        path,
        [
            _translation_event(run_id="run-a", created_at="2026-05-14T00:00:00+00:00", latency_ms=10),
            _translation_event(run_id="run-a", created_at="2026-05-14T00:00:03+00:00", status="filtered", filter_reason="stt_garbage", latency_ms=0, target_text=""),
            _translation_event(run_id="run-b", created_at="2026-05-14T00:01:00+00:00", latency_ms=30),
        ],
    )

    report = analyze_runtime_events(path, labels_path=labels_path)

    runs = {run["run_id"]: run for run in report["runs"]}
    assert set(runs) == {"run-a", "run-b"}
    assert runs["run-a"]["label"] == "commute_radio_control"
    assert runs["run-a"]["note"] == "same streamer, low-confusion control"
    assert runs["run-a"]["translation_events"] == 2
    assert runs["run-a"]["duration_sec"] == 3
    assert runs["run-a"]["status_breakdown"] == {
        "total": 2,
        "success": 1,
        "filtered": 1,
        "failed": 0,
        "other": 0,
    }
    assert runs["run-b"]["label"] == "singing_stress"


def test_run_summaries_include_success_latency_and_template_hits(tmp_path):
    path = tmp_path / "runtime_events_20260514.jsonl"
    _write_jsonl(
        path,
        [
            _translation_event(
                run_id="run-template",
                status="success",
                latency_ms=100,
                source_text="시청해주셔서 감사합니다.",
                target_text="感謝收看。",
            ),
            _translation_event(
                run_id="run-template",
                status="filtered",
                filter_reason="stt_template_garbage",
                latency_ms=0,
                source_text="구독과 좋아요는 저에게 큰 힘이 됩니다.",
                target_text="",
            ),
            _translation_event(
                run_id="run-template",
                status="success",
                latency_ms=300,
                source_text="안녕하세요",
                target_text="你好",
            ),
        ],
    )

    report = analyze_runtime_events(path, top_n=5)
    run = report["runs"][0]

    assert run["success_latency_ms"]["count"] == 2
    assert run["success_latency_ms"]["avg"] == 200
    assert run["template_hits"]["total"] == 2
    assert {"value": "success", "count": 1} in run["template_hits"]["by_status"]
    assert {"value": "filtered", "count": 1} in run["template_hits"]["by_status"]
    assert run["template_hits"]["by_filter_reason"] == [
        {"value": "stt_template_garbage", "count": 1}
    ]
    assert len(run["template_success_samples"]) == 1
    assert run["template_success_samples"][0]["source_text"] == "시청해주셔서 감사합니다."
