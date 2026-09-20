from __future__ import annotations

import json
from pathlib import Path

from scripts.suggest_identity_ocr_aliases import analyze, render_markdown


def _write_events(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def _unknown(observed: str, run_id: str) -> dict[str, object]:
    return {
        "event_type": "profile_resolution",
        "identity_authority": "calibrated_channel_identity_roi",
        "identity_read_attempted": True,
        "reason": "identity_blank_or_not_reviewed",
        "normalized_observed_identity": observed,
        "run_id": run_id,
    }


def test_repeated_unique_near_match_is_review_only_and_never_activates(tmp_path):
    events = tmp_path / "events.jsonl"
    _write_events(events, [
        _unknown("람코_", "run-a"),
        _unknown("람코_", "run-b"),
    ])

    report = analyze([events])

    row = next(item for item in report["candidates"] if item["observed"] == "람코_")
    assert row["status"] == "review"
    assert row["candidate_marker_id"] == "url_member_ranko"
    assert row["production_action"] == "manual_review_only"
    assert report["policy"]["runtime_activation"] == "none"
    assert report["policy"]["registry_mutation"] == "none"


def test_single_observation_and_unsafe_free_text_fail_closed(tmp_path):
    events = tmp_path / "events.jsonl"
    _write_events(events, [
        _unknown("람코_", "run-a"),
        _unknown("마냥_ [UR:L] unrelated prose", "run-a"),
    ])

    report = analyze([events])

    row = next(item for item in report["candidates"] if item["observed"] == "람코_")
    assert row["status"] == "insufficient"
    assert all("unrelated prose" not in item["observed"] for item in report["candidates"])


def test_existing_reviewed_alias_is_not_resuggested_and_report_is_compact(tmp_path):
    events = tmp_path / "events.jsonl"
    _write_events(events, [
        _unknown("솜망", "run-a"),
        _unknown("솜망", "run-b"),
        _unknown("솨주먹", "run-a"),
        _unknown("솨주먹", "run-b"),
    ])

    report = analyze([events])
    markdown = render_markdown(report)

    assert all(item["observed"] != "솜망" for item in report["candidates"])
    assert "`솨주먹`" in markdown
    assert "does not activate aliases" in markdown


def test_requires_calibrated_roi_provenance_and_deduplicates_input_paths(tmp_path):
    events = tmp_path / "events.jsonl"
    untrusted = _unknown("람코_", "run-a")
    untrusted["identity_authority"] = "whole_scene"
    _write_events(events, [
        _unknown("람코_", "run-a"),
        untrusted,
    ])

    report = analyze([events, events.resolve()])

    row = next(item for item in report["candidates"] if item["observed"] == "람코_")
    assert row["count"] == 1
    assert row["status"] == "insufficient"
    assert len(report["event_files"]) == 1
