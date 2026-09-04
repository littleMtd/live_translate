import json

from modules.profile_context import (
    ProfileState,
    load_registry_snapshot,
    profile_resolution_status,
)
from modules.profile_control import ProfileControlWatcher


def _write_registry(path, *, term="one"):
    path.write_text(json.dumps({
        "common_stt_terms": [],
        "profiles": [
            {"profile_id": "", "label": "General"},
            {"profile_id": "url", "label": "UR:L", "stt_terms": [term]},
            {"profile_id": "isegye_lilpa", "label": "Lilpa"},
        ],
    }), encoding="utf-8")


def _write_config(path, *, source="url", mode="auto"):
    path.write_text(json.dumps({
        "translation": {
            "streamer_profile": source,
            "profile_mode": mode,
            "use_profile": True,
        },
        "stt": {"use_profile_glossary": True},
    }), encoding="utf-8")


def test_dashboard_profile_change_hot_reloads_atomically(tmp_path):
    registry_path = tmp_path / "profiles.json"
    config_path = tmp_path / "config.json"
    status_path = tmp_path / "status.json"
    _write_registry(registry_path)
    _write_config(config_path)
    state = ProfileState(
        load_registry_snapshot(registry_path, version=1),
        source_profile_id="url",
    )
    watcher = ProfileControlWatcher(
        state=state,
        config_path=config_path,
        registry_path=registry_path,
        status_path=status_path,
    )
    before = state.current()
    _write_config(config_path, source="isegye_lilpa", mode="manual")
    watcher.poll_once()
    after = state.current()
    assert after.source_profile_id == "isegye_lilpa"
    assert after.effective_profile_id == "isegye_lilpa"
    assert after.mode == "manual"
    assert after.generation == before.generation + 1
    assert json.loads(status_path.read_text(encoding="utf-8"))["effective_profile_id"] == "isegye_lilpa"


def test_invalid_dashboard_reload_retains_previous_generation(tmp_path):
    registry_path = tmp_path / "profiles.json"
    config_path = tmp_path / "config.json"
    _write_registry(registry_path)
    _write_config(config_path)
    state = ProfileState(
        load_registry_snapshot(registry_path, version=1),
        source_profile_id="url",
    )
    watcher = ProfileControlWatcher(
        state=state,
        config_path=config_path,
        registry_path=registry_path,
        status_path=tmp_path / "status.json",
    )
    before = state.current()
    _write_config(config_path, source="invented")
    watcher.poll_once()
    assert state.current() == before


def test_registry_hot_reload_rejects_removing_active_profile(tmp_path):
    registry_path = tmp_path / "profiles.json"
    config_path = tmp_path / "config.json"
    _write_registry(registry_path)
    _write_config(config_path)
    state = ProfileState(
        load_registry_snapshot(registry_path, version=1),
        source_profile_id="url",
    )
    watcher = ProfileControlWatcher(
        state=state,
        config_path=config_path,
        registry_path=registry_path,
        status_path=tmp_path / "status.json",
    )
    before = state.current()
    registry_path.write_text(json.dumps({
        "common_stt_terms": [],
        "profiles": [{"profile_id": "", "label": "General"}],
    }), encoding="utf-8")
    watcher.poll_once()
    assert state.current() == before
    assert state.registry.version == 1


def test_dashboard_status_includes_latest_resolver_observation(tmp_path):
    registry_path = tmp_path / "profiles.json"
    config_path = tmp_path / "config.json"
    status_path = tmp_path / "status.json"
    _write_registry(registry_path)
    _write_config(config_path)
    state = ProfileState(
        load_registry_snapshot(registry_path, version=1),
        source_profile_id="url",
    )
    profile_resolution_status.replace(
        resolver_state="candidate",
        last_detection_at="2026-09-02T00:00:00+00:00",
        candidate_profile_id="isegye_lilpa",
        matched_markers=["isegye_brand_group"],
        marker_strengths=["medium"],
        status="candidate",
        reason="",
        state_transition="request_to_candidate",
        latency_ms=700.0,
        schema_retry_count=0,
        window_generation=3,
        registry_generation=1,
    )
    watcher = ProfileControlWatcher(
        state=state,
        config_path=config_path,
        registry_path=registry_path,
        status_path=status_path,
    )
    watcher.publish_status()
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["profile_resolver_state"] == "candidate"
    assert payload["profile_evidence_markers"] == ["isegye_brand_group"]
    assert payload["profile_last_detection_at"] == "2026-09-02T00:00:00+00:00"
