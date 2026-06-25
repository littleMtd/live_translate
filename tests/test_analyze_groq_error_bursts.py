from scripts.analyze_groq_error_bursts import (
    _is_generic_error,
    build_report,
    classify_error_utterance_outcomes,
    find_error_runs,
    lost_utterance_run_lengths,
)


def _stt(status, reason="", ts="2026-06-24T00:00:00+00:00", **kw):
    return {"event_type": "stt", "status": status, "reason": reason,
            "created_at": ts, "engine": "groq", **kw}


def test_is_generic_error_only_failed_error():
    assert _is_generic_error(_stt("failed", "error"))
    assert not _is_generic_error(_stt("failed", "rate_limited"))
    assert not _is_generic_error(_stt("success"))
    assert not _is_generic_error(_stt("filtered", "avg_logprob"))


def test_find_error_runs_breaks_on_non_error():
    events = [
        _stt("failed", "error"),
        _stt("failed", "error"),
        _stt("success"),
        _stt("failed", "error"),
    ]
    runs = find_error_runs(events)
    assert [len(r) for r in runs] == [2, 1]


def test_build_report_counts_and_distribution(tmp_path):
    # one run with a 3-burst, isolated singletons broken by successes
    path = tmp_path / "runtime_events_test.jsonl"
    import json
    lines = [
        _stt("failed", "error", "2026-06-24T00:00:00+00:00", run_id="r1", latency_ms=1.0),
        _stt("failed", "error", "2026-06-24T00:00:05+00:00", run_id="r1", latency_ms=10000.0),
        _stt("failed", "error", "2026-06-24T00:00:10+00:00", run_id="r1", latency_ms=2.0),
        _stt("success", "", "2026-06-24T00:00:15+00:00", run_id="r1"),
        _stt("failed", "error", "2026-06-24T00:00:20+00:00", run_id="r1", latency_ms=3.0),
    ]
    path.write_text("\n".join(json.dumps(e) for e in lines), encoding="utf-8")

    report = build_report([str(path)])
    assert report["totals"]["generic_error_events"] == 4
    assert report["consecutive_error_run_length_distribution"] == {3: 1, 1: 1}
    assert report["engine_field_finding"]["engine_switch_field_present"] is False
    assert report["engine_field_finding"]["attempt_index_present"] == 0
    # rescued-vs-lost is not claimed as a single number
    assert report["error_outcome_determinability"]["rescued_vs_lost"] == "partially_determinable_from_utterance_linkage"
    assert report["error_outcome_determinability"]["generic_error_events"] == 4


def test_classify_attempts_distinguishes_rescued_and_lost_utterances():
    events = [
        _stt("failed", "error", utterance_id="utt-1"),
        _stt("success", utterance_id="utt-1"),
        _stt("failed", "error", utterance_id="utt-2"),
        _stt("failed", "error", utterance_id="utt-2"),
        _stt("failed", "error", utterance_id="utt-3"),
        _stt("success", utterance_id="utt-4"),
    ]

    outcomes = classify_error_utterance_outcomes(events)

    assert [outcome["outcome"] for outcome in outcomes] == [
        "rescued", "lost", "lost", "non_error"
    ]
    assert lost_utterance_run_lengths(outcomes) == [2]


def test_unlinked_error_attempt_remains_unknown():
    outcomes = classify_error_utterance_outcomes([_stt("failed", "error")])

    assert outcomes[0]["outcome"] == "unknown"
    assert lost_utterance_run_lengths(outcomes) == []


def test_report_counts_explicit_attempt_diagnostic_coverage(tmp_path):
    path = tmp_path / "runtime_events_test.jsonl"
    import json
    event = _stt(
        "failed",
        "error",
        run_id="r1",
        utterance_id="utt-1",
        attempt_index=1,
        key_role="primary",
        will_retry=True,
    )
    path.write_text(json.dumps(event), encoding="utf-8")

    report = build_report([str(path)])

    finding = report["engine_field_finding"]
    assert finding["attempt_index_present"] == 1
    assert finding["key_role_present"] == 1
    assert finding["will_retry_present"] == 1
