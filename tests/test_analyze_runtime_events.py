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


def test_analyzer_defaults_to_live_and_can_include_all_run_kinds(tmp_path):
    path = tmp_path / "runtime_events_20260514.jsonl"
    _write_jsonl(
        path,
        [
            _translation_event(schema_version=3, run_id="live-run", run_kind="live"),
            _translation_event(schema_version=3, run_id="test-run", run_kind="test"),
            _translation_event(schema_version=2, run_id="legacy-run"),
        ],
    )

    live_report = analyze_runtime_events(path)
    all_report = analyze_runtime_events(path, run_kind="all")

    assert live_report["translation_events"] == 2
    assert live_report["run_ids"] == ["legacy-run", "live-run"]
    assert live_report["unfiltered_total_events"] == 3
    assert all_report["translation_events"] == 3
    assert {run["run_kind"] for run in all_report["runs"]} == {"live", "test"}


def test_run_id_filter_pins_report_to_one_run(tmp_path):
    path = tmp_path / "runtime_events_20260711.jsonl"
    _write_jsonl(
        path,
        [
            _translation_event(schema_version=3, run_id="20260711T010000Z-1", run_kind="live"),
            _translation_event(schema_version=3, run_id="20260711T020000Z-2", run_kind="live"),
            _translation_event(schema_version=2, run_id="20260710T230000Z-0"),
        ],
    )

    pinned = analyze_runtime_events(path, run_id="20260711T010000Z-1")
    latest = analyze_runtime_events(path, run_id="latest")

    assert pinned["run_id_filter"] == "20260711T010000Z-1"
    assert pinned["translation_events"] == 1
    assert pinned["run_ids"] == ["20260711T010000Z-1"]
    # "latest" resolves to the newest run after the run-kind filter.
    assert latest["run_id_filter"] == "20260711T020000Z-2"
    assert latest["translation_events"] == 1
    # No filter keeps the day aggregate (legacy counts as live).
    assert analyze_runtime_events(path)["translation_events"] == 3


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
                "quality_classifications": ["target_high_latin_approved_only"],
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
    assert report["quality_classifications"] == [
        {"classification": "target_high_latin_approved_only", "count": 1}
    ]
    assert report["latency_ms"]["avg"] == 55
    assert len(report["latest"]) == 1
    assert len(report["flagged_samples"]) == 1


def test_analyzer_summarizes_record_only_source_fuzzy_shadow(tmp_path):
    path = tmp_path / "runtime_events_20260725.jsonl"
    unique = {
        "schema": 1,
        "mode": "record_only",
        "enabled": True,
        "eligible": True,
        "applied": False,
        "profile_id": "hades_chxxnnx",
        "reason": "candidates_observed",
        "candidate_count": 1,
        "unique_match_count": 1,
        "ambiguous_count": 0,
        "would_change": True,
        "proposed_text": "챈나",
        "candidates": [
            {
                "observed": "채나",
                "canonical": "챈나",
                "best_candidate": "챈나",
                "decision": "unique_match",
            }
        ],
    }
    ambiguous = {
        **unique,
        "unique_match_count": 0,
        "ambiguous_count": 1,
        "would_change": False,
        "proposed_text": "가나",
        "candidates": [
            {
                "observed": "가나",
                "canonical": "",
                "best_candidate": "각나",
                "decision": "ambiguous",
            }
        ],
    }
    _write_jsonl(
        path,
        [
            _translation_event(
                run_id="run-shadow",
                source_text="채나",
                source_fuzzy_shadow=unique,
            ),
            _translation_event(
                run_id="run-shadow",
                source_text="가나",
                source_fuzzy_shadow=ambiguous,
            ),
            _translation_event(run_id="run-legacy"),
        ],
    )

    report = analyze_runtime_events(path)
    summary = report["source_fuzzy_shadow"]

    assert summary["observed_events"] == 2
    assert summary["coverage_rate"] == 0.6667
    assert summary["candidate_events"] == 2
    assert summary["candidate_count"] == 2
    assert summary["unique_match_count"] == 1
    assert summary["ambiguous_count"] == 1
    assert summary["would_change_events"] == 1
    assert summary["applied_count"] == 0
    assert summary["diagnostic_errors"] == 0
    assert summary["pairs"][0]["decision"] in {"unique_match", "ambiguous"}
    assert report["runs"][0]["source_fuzzy_shadow"]["observed_events"] in {0, 2}
    shadow_run = next(run for run in report["runs"] if run["run_id"] == "run-shadow")
    assert shadow_run["source_fuzzy_shadow"]["candidate_count"] == 2


def test_analyzer_summarizes_sentence_hold_shadow_opportunities(tmp_path):
    path = tmp_path / "runtime_events_20260725.jsonl"
    _write_jsonl(
        path,
        [
            {
                "event_type": "sentence_hold_shadow",
                "run_id": "run-x",
                "phase": "candidate",
                "shadow_id": "sentence-hold-1",
                "signals": ["unfinished_connector"],
                "disposition": "emitted",
                "cut_reason": "forced_blob",
                "candidate_text": "게임 하고",
            },
            {
                "event_type": "sentence_hold_shadow",
                "run_id": "run-x",
                "phase": "outcome",
                "shadow_id": "sentence-hold-1",
                "signals": ["unfinished_connector"],
                "observed_next_chunk": True,
                "next_chunk_delay_ms": 420,
                "within_300ms": False,
                "within_500ms": True,
                "raw_continuation_heuristic": True,
                "useful_merge_heuristic": True,
                "outcome_reason": "next_stt_chunk",
                "next_chunk_text": "있어요",
            },
        ],
    )

    summary = analyze_runtime_events(path)["sentence_hold_shadow"]

    assert summary["candidate_count"] == 1
    assert summary["outcome_count"] == 1
    assert summary["one_chunk_useful_rate"] == 1.0
    assert summary["useful_within_300ms_rate"] == 0.0
    assert summary["useful_within_500ms_rate"] == 1.0
    assert summary["next_chunk_delay_ms"]["p50"] == 420
    assert summary["by_signal"] == [
        {"signal": "unfinished_connector", "count": 1}
    ]
    assert summary["actionable_emitted"]["useful_within_500ms_rate"] == 1.0


def test_sentence_hold_summary_scopes_ids_and_excludes_bad_pairs_from_rates(tmp_path):
    path = tmp_path / "runtime_events_20260725.jsonl"

    def candidate(run_id, disposition="emitted"):
        return {
            "event_type": "sentence_hold_shadow",
            "run_id": run_id,
            "phase": "candidate",
            "shadow_id": "sentence-hold-1",
            "signals": ["unfinished_connector"],
            "disposition": disposition,
            "cut_reason": "forced_blob",
        }

    def outcome(run_id):
        return {
            "event_type": "sentence_hold_shadow",
            "run_id": run_id,
            "phase": "outcome",
            "shadow_id": "sentence-hold-1",
            "signals": ["unfinished_connector"],
            "observed_next_chunk": True,
            "next_chunk_delay_ms": 200,
            "within_300ms": True,
            "within_500ms": True,
            "raw_continuation_heuristic": True,
            "useful_merge_heuristic": True,
            "outcome_reason": "next_stt_chunk",
        }

    _write_jsonl(
        path,
        [
            candidate("run-a"),
            outcome("run-a"),
            outcome("run-a"),  # duplicate must not multiply the numerator
            candidate("run-b", "buffered"),
            candidate("run-b", "buffered"),  # duplicate candidate
            outcome("run-b"),
            outcome("run-c"),  # orphan outcome
            candidate("run-d"),  # unresolved candidate
        ],
    )

    summary = analyze_runtime_events(path)["sentence_hold_shadow"]

    assert summary["candidate_count"] == 3
    assert summary["candidate_event_count"] == 4
    assert summary["outcome_event_count"] == 4
    assert summary["matched_outcome_count"] == 2
    assert summary["unresolved_count"] == 1
    assert summary["orphan_outcome_count"] == 1
    assert summary["duplicate_candidate_count"] == 1
    assert summary["duplicate_outcome_count"] == 1
    assert summary["one_chunk_useful_count"] == 2
    assert summary["one_chunk_useful_rate"] == 0.6667
    assert summary["actionable_emitted"]["candidate_count"] == 2
    assert summary["actionable_emitted"]["useful_within_500ms_rate"] == 0.5
    assert summary["already_buffered"]["candidate_count"] == 1
    assert summary["already_buffered"]["useful_within_500ms_rate"] == 1.0


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


def _stt_event(**overrides):
    base = {
        "event_type": "stt",
        "run_id": "run-x",
        "created_at": "2026-05-14T00:00:00+00:00",
        "engine": "groq",
        "model": "whisper-large-v3",
        "status": "success",
        "reason": "",
        "request_sent": True,
        "audio_seconds": 4.0,
        "latency_ms": 250,
        "text_len": 12,
    }
    base.update(overrides)
    return base


def _audio_event(**overrides):
    base = {
        "event_type": "audio",
        "run_id": "run-x",
        "created_at": "2026-05-14T00:00:00+00:00",
        "stage": "vad",
        "cut_reason": "silence",
        "audio_seconds": 5.0,
        "raw_audio_seconds": 4.6,
        "overlap_seconds": 0.4,
        "adaptive_active": False,
    }
    base.update(overrides)
    return base


def _fallback_event(**overrides):
    base = {
        "event_type": "translation_fallback",
        "run_id": "run-x",
        "created_at": "2026-05-14T00:00:00+00:00",
        "action": "probe_succeeded",
        "probe_status": "success",
        "probe_elapsed_ms": 250,
        "probe_history_items": 5,
        "probe_success_streak": 1,
        "state_applied": True,
    }
    base.update(overrides)
    return base


def test_fallback_summary_reports_circuit_and_probe_transitions(tmp_path):
    path = tmp_path / "runtime_events_20260514.jsonl"
    _write_jsonl(
        path,
        [
            _translation_event(run_id="run-circuit"),
            _fallback_event(
                run_id="run-circuit",
                action="circuit_opened",
                probe_status="",
                failure_scope="provider",
                api_error_type="timeout",
                api_error_message_class="read_timeout",
            ),
            _fallback_event(
                run_id="run-circuit",
                action="probe_cooldown_skipped",
                probe_status="cooldown_skipped",
                probe_elapsed_ms=0,
            ),
            _fallback_event(run_id="run-circuit"),
            _fallback_event(
                run_id="run-circuit",
                action="probe_failed",
                probe_status="empty",
                probe_elapsed_ms=5000,
                probe_success_streak=0,
            ),
            _fallback_event(
                run_id="run-circuit",
                probe_success_streak=2,
                probe_elapsed_ms=900,
            ),
            _fallback_event(
                run_id="run-circuit",
                action="circuit_closed",
                probe_success_streak=2,
                probe_elapsed_ms=900,
            ),
        ],
    )

    report = analyze_runtime_events(path)

    self_summary = report["translation_fallback"]
    assert report["translation_fallback_events"] == 6
    assert self_summary["circuits_opened"] == 1
    assert self_summary["circuit_open_by_failure_scope"] == [
        {"value": "provider", "count": 1}
    ]
    assert self_summary["circuit_open_by_error_type"] == [
        {"value": "timeout", "count": 1}
    ]
    assert self_summary["circuit_open_by_error_message_class"] == [
        {"value": "read_timeout", "count": 1}
    ]
    assert self_summary["circuits_closed"] == 1
    assert self_summary["probe_attempts"] == 3
    assert self_summary["successful_probes"] == 2
    assert self_summary["failed_probes"] == 1
    assert self_summary["cooldown_skips"] == 1
    assert self_summary["max_probe_success_streak"] == 2
    assert self_summary["probe_latency_ms"]["p50"] == 900
    assert self_summary["probe_history_items"]["p95"] == 5
    assert report["runs"][0]["translation_fallback"] == self_summary


def test_stt_summary_counts_requests_audio_and_reasons(tmp_path):
    path = tmp_path / "runtime_events_20260514.jsonl"
    _write_jsonl(
        path,
        [
            _translation_event(run_id="run-a"),
            _stt_event(run_id="run-a", status="success", request_sent=True, audio_seconds=4, latency_ms=250),
            _stt_event(run_id="run-a", status="failed", reason="rate_limited", request_sent=True, audio_seconds=5, latency_ms=20),
            _stt_event(run_id="run-a", status="skipped", reason="below_volume_threshold", request_sent=False, audio_seconds=3, latency_ms=1),
        ],
    )

    report = analyze_runtime_events(path)

    assert report["stt_events"] == 3
    assert report["stt_summary"]["total"] == 3
    assert report["stt_summary"]["requests_sent"] == 2
    assert report["stt_summary"]["request_budget"]["limit"] >= 2000
    assert report["stt_summary"]["request_budget"]["used"] == 2
    assert report["stt_summary"]["request_budget"]["remaining"] >= 1998
    assert report["stt_summary"]["audio_seconds_total"] == 12
    assert report["stt_summary"]["audio_seconds_sent"] == 9
    assert {"value": "rate_limited", "count": 1} in report["stt_summary"]["by_reason"]
    assert {"value": "below_volume_threshold", "count": 1} in report["stt_summary"]["by_reason"]
    assert report["runs"][0]["stt"]["requests_sent"] == 2


def test_audio_summary_counts_vad_cut_reasons(tmp_path):
    path = tmp_path / "runtime_events_20260514.jsonl"
    _write_jsonl(
        path,
        [
            _translation_event(run_id="run-a"),
            _audio_event(run_id="run-a", cut_reason="silence", audio_seconds=5, overlap_seconds=0.4),
            _audio_event(run_id="run-a", cut_reason="hard_max", audio_seconds=9, adaptive_active=True, overlap_seconds=1.0),
        ],
    )

    report = analyze_runtime_events(path)

    assert report["audio_events"] == 2
    assert report["audio_summary"]["total"] == 2
    assert {"value": "silence", "count": 1} in report["audio_summary"]["by_cut_reason"]
    assert {"value": "hard_max", "count": 1} in report["audio_summary"]["by_cut_reason"]
    assert {"value": "True", "count": 1} in report["audio_summary"]["by_adaptive_active"]
    assert report["audio_summary"]["audio_seconds"]["max"] == 9
    assert report["runs"][0]["audio"]["total"] == 2


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
            _translation_event(status="filtered", filter_reason="stt_low_value_fragment", target_text=""),
            _translation_event(status="filtered", filter_reason="stt_song_fragment", target_text=""),
            _translation_event(status="success"),  # no filter_reason → excluded from this aggregation
        ],
    )

    report = analyze_runtime_events(path)

    reasons = {item["value"]: item["count"] for item in report["by_filter_reason"]}
    assert reasons == {
        "too_long": 2,
        "too_short": 1,
        "duplicate": 1,
        "stt_low_value_fragment": 1,
        "stt_song_fragment": 1,
    }


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


def test_queue_observability_summaries_include_retry_and_dependency_marker(tmp_path):
    path = tmp_path / "runtime_events_20260514.jsonl"
    _write_jsonl(
        path,
        [
            _translation_event(
                engine_latency_ms=100,
                queue_wait_ms=0,
                output_delay_ms=120,
                predecessor_stall_ms=20,
                retry_count=1,
                retry_reason="timeout",
                api_attempt_count=2,
                api_timeout_count=1,
                api_total_wall_ms=12000,
                api_final_attempt_ms=1500,
                api_first_attempt_ms=10000,
                api_retry_attempt_ms=1500,
                retry_sleep_ms=500,
                api_attempt_timeout_ms=10000,
                api_attempt_index=2,
                api_inflight_count_at_start=1,
                source_text_char_count=20,
                prompt_char_count=2000,
                request_body_char_count=2600,
                message_count=4,
                context_item_count=1,
                attempts=[
                    {
                        "phase": "fallback_chain",
                        "engine": "nvidia",
                        "status": "empty",
                        "api_timeout_count": 1,
                        "selected_for_output": False,
                    },
                    {
                        "phase": "fallback_chain",
                        "engine": "groq",
                        "status": "success",
                        "api_timeout_count": 0,
                        "api_cost_usd": 0.001,
                        "selected_for_output": True,
                    },
                ],
                quality_retry={
                    "trigger": "amount_mismatch",
                    "applied": True,
                    "reason": "selective_trigger_resolved",
                },
                starts_with_dependency_marker=True,
                dependency_marker="그래서",
            ),
            _translation_event(
                engine="openrouter",
                engine_latency_ms=200,
                queue_wait_ms=50,
                output_delay_ms=260,
                predecessor_stall_ms=10,
                retry_count=0,
                retry_reason="",
                api_attempt_count=1,
                api_timeout_count=0,
                api_total_wall_ms=180,
                api_final_attempt_ms=180,
                api_first_attempt_ms=180,
                api_retry_attempt_ms=None,
                retry_sleep_ms=0,
                api_attempt_timeout_ms=10000,
                api_attempt_index=1,
                api_inflight_count_at_start=0,
                source_text_char_count=10,
                prompt_char_count=1000,
                request_body_char_count=1400,
                message_count=2,
                context_item_count=0,
                api_cost_usd=0.002,
                starts_with_dependency_marker=False,
                dependency_marker="",
            ),
        ],
    )

    report = analyze_runtime_events(path)

    assert report["queue_latency_ms"]["engine_latency_ms"]["count"] == 2
    assert report["queue_latency_ms"]["queue_wait_ms"]["max"] == 50
    assert report["queue_latency_ms"]["output_delay_ms"]["p95"] == 260
    assert report["queue_latency_ms"]["predecessor_stall_ms"]["p50"] == 20
    assert report["retry_summary"]["retry_events"] == 1
    assert report["retry_summary"]["retry_rate"] == 0.5
    assert report["retry_summary"]["by_retry_reason"] == [{"value": "timeout", "count": 1}]
    assert report["retry_summary"]["quality_retry"] == {
        "events": 1,
        "rate": 0.5,
        "applied": 1,
        "by_trigger": [{"value": "amount_mismatch", "count": 1}],
        "by_reason": [{"value": "selective_trigger_resolved", "count": 1}],
    }
    assert report["api_diagnostics"]["api_events"] == 2
    assert report["api_diagnostics"]["timeout_events"] == 1
    assert report["api_diagnostics"]["timeout_rate"] == 0.5
    assert report["api_diagnostics"]["long_api_ge_10s"] == 1
    assert report["api_diagnostics"]["long_api_ge_10s_timeout_events"] == 1
    assert report["api_diagnostics"]["cost_usd"] == {
        "observations": 2,
        "total": 0.003,
        "by_engine": [
            {"engine": "groq", "cost_usd": 0.001},
            {"engine": "openrouter", "cost_usd": 0.002},
        ],
    }
    assert report["api_diagnostics"]["fields"]["api_total_wall_ms"]["max"] == 12000
    assert report["api_diagnostics"]["fields"]["api_attempt_timeout_ms"]["p50"] == 10000
    assert report["api_diagnostics"]["fields"]["api_attempt_index"]["max"] == 2
    assert report["api_diagnostics"]["fields"]["prompt_char_count"]["p50"] == 2000
    assert report["api_diagnostics"]["fields"]["request_body_char_count"]["max"] == 2600
    chain = report["api_diagnostics"]["attempt_chain"]
    assert chain["events_with_chain"] == 1
    assert chain["total_attempts"] == 2
    assert chain["fallback_chain_attempts"] == 2
    assert chain["selected_attempts"] == 1
    assert chain["by_engine"] == [
        {"value": "nvidia", "count": 1},
        {"value": "groq", "count": 1},
    ]
    assert report["dependency_markers"]["marker_events"] == 1
    assert report["dependency_markers"]["marker_ratio"] == 0.5
    assert report["dependency_markers"]["by_marker"] == [{"value": "그래서", "count": 1}]
    assert any("poll-gap" in note for note in report["analyzer_output_notes"])


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


def test_run_summaries_include_queue_observability_breakdown(tmp_path):
    path = tmp_path / "runtime_events_20260514.jsonl"
    _write_jsonl(
        path,
        [
            _translation_event(
                run_id="run-a",
                engine_latency_ms=10,
                queue_wait_ms=0,
                output_delay_ms=12,
                predecessor_stall_ms=2,
                retry_count=1,
                retry_reason="network",
                starts_with_dependency_marker=True,
                dependency_marker="근데",
            ),
            _translation_event(
                run_id="run-a",
                engine_latency_ms=30,
                queue_wait_ms=20,
                output_delay_ms=70,
                predecessor_stall_ms=40,
                retry_count=0,
                starts_with_dependency_marker=False,
            ),
            _translation_event(
                run_id="run-b",
                engine_latency_ms=100,
                queue_wait_ms=5,
                output_delay_ms=110,
                predecessor_stall_ms=5,
                retry_count=0,
            ),
        ],
    )

    report = analyze_runtime_events(path)
    runs = {run["run_id"]: run for run in report["runs"]}

    assert runs["run-a"]["queue_latency_ms"]["engine_latency_ms"]["p95"] == 30
    assert runs["run-a"]["queue_latency_ms"]["predecessor_stall_ms"]["p99"] == 40
    assert runs["run-a"]["retry_summary"]["retry_rate"] == 0.5
    assert runs["run-a"]["dependency_markers"]["marker_ratio"] == 0.5
    assert runs["run-b"]["queue_latency_ms"]["queue_wait_ms"]["p95"] == 5
    assert runs["run-b"]["retry_summary"]["retry_rate"] == 0.0


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
