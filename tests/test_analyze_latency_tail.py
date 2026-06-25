import json

import pytest

from scripts.analyze_latency_tail import build_report, parse_args


def _t(latency, engine, eng_lat=None, **kw):
    return {
        "event_type": "translation", "schema_version": 2, "status": "success",
        "latency_ms": latency, "engine": engine,
        "engine_latency_ms": eng_lat if eng_lat is not None else latency, **kw,
    }


def test_build_report_flags_wall_time_gap_without_claiming_single_engine(tmp_path):
    rows = [_t(1000, "nvidia") for _ in range(95)]
    # tail: openrouter calls far above its 8s configured timeout, single attempt
    rows += [
        _t(
            90000,
            "openrouter",
            api_attempt_count=1,
            api_timeout_count=0,
            api_total_wall_ms=89000,
        )
        for _ in range(5)
    ]
    path = tmp_path / "runtime_events_test.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    report = build_report([str(path)], tail_quantile=0.95)

    assert report["per_engine"]["openrouter"]["timeout_appears_unenforced"] is True
    assert report["per_engine"]["openrouter"]["configured_timeout_s"] == 8
    # The worker elapsed field alone is not treated as proof of a single-engine call;
    # final API diagnostics provide the bounded attribution instead.
    dom = report["tail_domination"]
    assert dom["worker_elapsed_field_~=_translation_latency"] >= dom["predecessor_stall_gt_50pct"]
    assert dom["final_api_wall_gt_80pct"] == 5
    assert dom["had_api_timeout"] == 0
    assert report["code_verification"]["openrouter_timeout_is_wired"] is True
    assert report["code_verification"]["original_candidate_fix_status"].startswith("falsified")
    assert report["openrouter_wall_time_gap"]["final_api_over_configured_socket_timeout"] == 5
    assert report["nvidia_retry_tradeoff"]["implementation_status"].startswith("proposal_only")


def test_overall_percentiles_present(tmp_path):
    rows = [_t(100 + i, "nvidia") for i in range(50)]
    path = tmp_path / "runtime_events_test.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    report = build_report([str(path)])
    assert "p99" in report["overall_latency_ms"]
    assert report["overall_latency_ms"]["n"] == 50


@pytest.mark.parametrize("quantile", [-0.01, 1.0, 1.5])
def test_tail_quantile_must_be_in_supported_range(quantile):
    with pytest.raises(ValueError, match="tail_quantile"):
        build_report([], tail_quantile=quantile)


def test_cli_rejects_unsupported_tail_quantile():
    with pytest.raises(SystemExit):
        parse_args(["--events", "events.jsonl", "--tail-quantile", "1"])
