import json

import pytest

from scripts.analyze_latency_tail import build_report, parse_args


def _t(latency, engine, eng_lat=None, *, schema_version=6, attempts=None, **kw):
    return {
        "event_type": "translation", "schema_version": schema_version, "status": "success",
        "latency_ms": latency, "engine": engine,
        "engine_latency_ms": eng_lat if eng_lat is not None else latency,
        "attempts": attempts or [], **kw,
    }


def test_build_report_uses_recorded_attempt_timeout_without_static_provider_config(tmp_path):
    rows = [_t(1000, "nvidia") for _ in range(95)]
    # Tail attempt exceeds its own recorded timeout; no repository config inference is needed.
    rows += [
        _t(
            90000,
            "deepseek",
            api_attempt_count=1,
            api_timeout_count=0,
            api_total_wall_ms=89000,
            attempts=[{
                "engine": "deepseek",
                "api_total_wall_ms": 89000,
                "timeout_config_ms": 4000,
            }],
        )
        for _ in range(5)
    ]
    path = tmp_path / "runtime_events_test.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    report = build_report([str(path)], tail_quantile=0.95)

    assert report["per_engine"]["deepseek"]["timeout_appears_unenforced"] is True
    assert report["per_engine"]["deepseek"]["recorded_timeout_ms"] == [4000.0]
    assert report["per_engine"]["deepseek"]["attempts_over_recorded_timeout_margin"] == 5
    # The worker elapsed field alone is not treated as proof of a single-engine call;
    # final API diagnostics provide the bounded attribution instead.
    dom = report["tail_domination"]
    assert dom["worker_elapsed_field_~=_translation_latency"] >= dom["predecessor_stall_gt_50pct"]
    assert dom["final_api_wall_gt_80pct"] == 5
    assert dom["had_api_timeout"] == 0
    assert report["timeout_evidence"]["source"] == "per-attempt runtime ledger"
    assert report["schema_version_distribution"] == {6: 100}


def test_overall_percentiles_present(tmp_path):
    rows = [_t(100 + i, "nvidia") for i in range(50)]
    path = tmp_path / "runtime_events_test.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    report = build_report([str(path)])
    assert "p99" in report["overall_latency_ms"]
    assert report["overall_latency_ms"]["n"] == 50


def test_legacy_schema_two_rows_remain_aggregate_only(tmp_path):
    path = tmp_path / "runtime_events_test.jsonl"
    path.write_text(json.dumps(_t(250, "nvidia", schema_version=2)), encoding="utf-8")

    report = build_report([str(path)], tail_quantile=0)

    assert report["overall_latency_ms"]["n"] == 1
    assert report["schema_version_distribution"] == {2: 1}
    assert report["per_engine"]["nvidia"]["comparable_attempt_timeout_coverage"] == 0.0
    assert report["per_engine"]["nvidia"]["timeout_appears_unenforced"] is False


def test_multi_retry_total_wall_is_not_compared_to_one_attempt_timeout(tmp_path):
    path = tmp_path / "runtime_events_test.jsonl"
    event = _t(
        9000,
        "nvidia",
        attempts=[{
            "engine": "nvidia",
            "api_attempt_count": 2,
            "api_total_wall_ms": 9000,
            "timeout_config_ms": 5000,
        }],
    )
    path.write_text(json.dumps(event), encoding="utf-8")

    report = build_report([str(path)], tail_quantile=0)

    engine = report["per_engine"]["nvidia"]
    assert engine["recorded_timeout_ms"] == [5000.0]
    assert engine["comparable_attempt_timeout_coverage"] == 0.0
    assert engine["timeout_appears_unenforced"] is False


@pytest.mark.parametrize("quantile", [-0.01, 1.0, 1.5])
def test_tail_quantile_must_be_in_supported_range(quantile):
    with pytest.raises(ValueError, match="tail_quantile"):
        build_report([], tail_quantile=quantile)


def test_cli_rejects_unsupported_tail_quantile():
    with pytest.raises(SystemExit):
        parse_args(["--events", "events.jsonl", "--tail-quantile", "1"])
