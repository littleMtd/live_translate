"""The owner's settings live in one untracked local_settings.json; dashboard edits are saved into it."""
import json

import config
import settings_store
from config import _Config
from settings_store import _apply_dashboard_overrides, local_settings_path


def test_local_settings_override_profile_choice_only(tmp_path):
    path = tmp_path / "local_settings.json"
    path.write_text(json.dumps({
        "translation": {"streamer_profile": "stellive_hina", "profile_mode": "manual", "use_profile": True},
        "keys": {"deepseek": "not-allowed"},
    }), encoding="utf-8")
    base = _Config()
    applied = _apply_dashboard_overrides(base, path)
    assert (applied.translation.streamer_profile, applied.translation.profile_mode) == ("stellive_hina", "manual")
    assert applied.keys == base.keys                      # outside the whitelist: ignored


def test_invalid_local_settings_are_ignored(tmp_path):
    path = tmp_path / "local_settings.json"
    path.write_text(json.dumps({"translation": {"streamer_profile": "nobody", "profile_mode": "sometimes"}}),
                    encoding="utf-8")
    base = _Config()
    assert _apply_dashboard_overrides(base, path).translation == base.translation
    path.write_text("{not json", encoding="utf-8")
    assert _apply_dashboard_overrides(base, path) is base


def test_path_switch_and_tests_never_read_the_owner_file(monkeypatch, tmp_path):
    monkeypatch.delenv("LIVE_TRANSLATE_LOCAL_SETTINGS", raising=False)
    assert local_settings_path() == settings_store._LOCAL_SETTINGS_JSON
    monkeypatch.setenv("LIVE_TRANSLATE_LOCAL_SETTINGS", str(tmp_path / "x.json"))
    assert local_settings_path() == tmp_path / "x.json"
    monkeypatch.setenv("LIVE_TRANSLATE_LOCAL_SETTINGS", "")
    assert local_settings_path() is None


def test_suite_runs_with_checked_in_defaults():
    # conftest disables local settings, so request-byte baselines never see a local profile
    assert config.cfg.translation.streamer_profile == _Config().translation.streamer_profile
    assert config.cfg.translation.profile_mode == _Config().translation.profile_mode


def test_config_import_applies_local_settings_and_reports_it(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    path = tmp_path / "local_settings.json"
    path.write_text(json.dumps({"translation": {"streamer_profile": "stellive_hina", "profile_mode": "manual"}}),
                    encoding="utf-8")
    probe = ("import config; t = config.cfg.translation; "
             "print(t.streamer_profile, t.profile_mode, config.LOCAL_SETTINGS_APPLIED)")
    repo = Path(config.__file__).parent

    dashboard = tmp_path / "live_translate_config.json"

    def run(**env):
        base = {k: v for k, v in os.environ.items() if k != "LIVE_TRANSLATE_APPLY_DASHBOARD_CONFIG"}
        out = subprocess.run([sys.executable, "-c", probe], cwd=repo, capture_output=True, text=True,
                             env={**base, "LIVE_TRANSLATE_DASHBOARD_CONFIG": str(dashboard), **env},
                             check=True)
        return out.stdout.split()

    assert run(LIVE_TRANSLATE_LOCAL_SETTINGS=str(path)) == ["stellive_hina", "manual", "True"]
    # the dashboard changed the mode since the last export: that edit wins over the file
    dashboard.write_text(json.dumps({"translation": {"streamer_profile": "stellive_hina",
                                                     "profile_mode": "auto"}}), encoding="utf-8")
    snapshot = tmp_path / "live_translate_config.exported.json"
    snapshot.write_text(json.dumps({"translation.streamer_profile": "stellive_hina",
                                    "translation.profile_mode": "manual"}), encoding="utf-8")
    assert run(LIVE_TRANSLATE_LOCAL_SETTINGS=str(path))[:2] == ["stellive_hina", "auto"]
    # an untouched (older) dashboard file never undoes a hand edit of local_settings.json
    snapshot.write_text(json.dumps({"translation.streamer_profile": "stellive_hina",
                                    "translation.profile_mode": "auto"}), encoding="utf-8")
    assert run(LIVE_TRANSLATE_LOCAL_SETTINGS=str(path))[:2] == ["stellive_hina", "manual"]
    snapshot.unlink()
    # all-invalid file: nothing applied, nothing reported
    path.write_text(json.dumps({"translation": {"streamer_profile": "nobody"}}), encoding="utf-8")
    assert run(LIVE_TRANSLATE_LOCAL_SETTINGS=str(path))[2] == "False"


def test_dashboard_edits_are_measured_against_the_export_snapshot(tmp_path):
    dashboard, snapshot = tmp_path / "d.json", tmp_path / "s.json"
    dashboard.write_text(json.dumps({"subtitle": {"alpha": 0.5, "font_size": 30, "bg": "#000"},
                                     "translation": {"profile_mode": "auto"}}), encoding="utf-8")
    snapshot.write_text(json.dumps({"subtitle.alpha": 1.0, "subtitle.font_size": 30,
                                    "translation.profile_mode": "auto"}), encoding="utf-8")
    assert settings_store.dashboard_edits(dashboard, snapshot) == ({"subtitle.alpha": 0.5}, {})
    # no snapshot yet (first run): nothing counts as an edit
    assert settings_store.dashboard_edits(dashboard, tmp_path / "absent.json") == ({}, {})


def test_stale_dashboard_write_back_never_undoes_a_hand_edit(monkeypatch, tmp_path):
    from utils import config_export

    export = tmp_path / "live_translate_config.json"
    snapshot = tmp_path / "live_translate_config.exported.json"
    monkeypatch.setattr(config_export, "_EXPORT_PATH", export)
    config_export.write()                                         # alpha exported at 1.0
    base = json.loads(export.read_text(encoding="utf-8"))
    hand_edited = settings_store._apply_overrides(_Config(), {"subtitle": {"alpha": 0.8}})
    monkeypatch.setattr(config_export, "cfg", hand_edited)
    config_export.write()                                         # next start: alpha 0.8, history [1.0]
    # the dashboard (cached at 1.0) saves a profile change and writes alpha 1.0 back
    stale = {**base, "translation": {**base["translation"], "profile_mode": "manual"}}
    export.write_text(json.dumps(stale), encoding="utf-8")
    edits, old = settings_store.dashboard_edits(export, snapshot)
    assert edits == {"translation.profile_mode": "manual"} and old == {"subtitle.alpha": 1.0}


def _dashboard_save(export, section, name, value):
    data = json.loads(export.read_text(encoding="utf-8"))
    data[section][name] = value
    export.write_text(json.dumps(data), encoding="utf-8")


def test_switching_back_and_forth_in_the_dashboard_keeps_working(monkeypatch, tmp_path):
    from utils import config_export

    export = tmp_path / "live_translate_config.json"
    snapshot = tmp_path / "live_translate_config.exported.json"
    monkeypatch.setattr(config_export, "_EXPORT_PATH", export)
    current = _Config()
    monkeypatch.setattr(config_export, "cfg", current)
    config_export.write()
    for value in ("irise", "mwmeu", "irise", "mwmeu"):            # profile A -> B -> A -> B
        _dashboard_save(export, "translation", "streamer_profile", value)
        edits, stale = settings_store.dashboard_edits(export, snapshot)
        assert edits == {"translation.streamer_profile": value} and stale == {}
        # next start: the edit is applied and exported
        current = settings_store._apply_overrides(current, {"translation": {"streamer_profile": value}})
        monkeypatch.setattr(config_export, "cfg", current)
        monkeypatch.setattr(settings_store, "PENDING_DASHBOARD_EDITS", edits)
        config_export.write()
        monkeypatch.setattr(settings_store, "PENDING_DASHBOARD_EDITS", {})


def test_profile_flag_run_does_not_block_choosing_that_profile_later(monkeypatch, tmp_path):
    from utils import config_export

    export = tmp_path / "live_translate_config.json"
    snapshot = tmp_path / "live_translate_config.exported.json"
    monkeypatch.setattr(config_export, "_EXPORT_PATH", export)
    config_export.write()
    config_export.write({"streamer_profile": "irise", "profile_mode": "manual"})   # main.py --profile irise
    config_export.write()                                                          # normal start again
    _dashboard_save(export, "translation", "streamer_profile", "irise")
    assert settings_store.dashboard_edits(export, snapshot) == ({"translation.streamer_profile": "irise"}, {})


def test_invalid_dashboard_values_are_not_saved(tmp_path):
    dashboard, snapshot = tmp_path / "d.json", tmp_path / "s.json"
    dashboard.write_text(json.dumps({"audio": {"vad_silence_sec": 99}, "subtitle": {"font_size": 99}}),
                         encoding="utf-8")
    snapshot.write_text(json.dumps({"audio.vad_silence_sec": 0.9, "subtitle.font_size": 22}), encoding="utf-8")
    assert settings_store.dashboard_edits(dashboard, snapshot) == ({}, {})


def test_failed_export_leaves_no_snapshot(monkeypatch, tmp_path):
    import pytest
    from utils import config_export

    monkeypatch.setattr(config_export, "_EXPORT_PATH", tmp_path / "live_translate_config.json")
    config_export.write()
    real = config_export._atomic_write

    def fail_on_snapshot(path, text):
        if path.name.endswith(".exported.json"):
            raise OSError("disk full")
        real(path, text)

    monkeypatch.setattr(config_export, "_atomic_write", fail_on_snapshot)
    with pytest.raises(OSError):
        config_export.write({"streamer_profile": "stellive_hina", "profile_mode": "manual"})
    assert not (tmp_path / "live_translate_config.exported.json").exists()


def test_problems_are_reported_not_silently_dropped():
    data = {"_說明": "note", "translation": {"_x": "note", "profile_mode": "sometimes", "modle": "x"},
            "keys": {"deepseek": "secret"}, "subtitle": {"font_size": 99, "alpha": 0.5},
            "live_engine": "groq"}
    problems = settings_store.settings_problems(data, _Config())
    assert len(problems) == 5
    assert any("translation.profile_mode" in p for p in problems)
    assert any("translation.modle" in p and "不認得" in p for p in problems)
    assert any(p.startswith("keys") for p in problems) and any("font_size" in p for p in problems)
    assert any(p.startswith("live_engine") for p in problems)
    assert all("secret" not in p for p in problems)


def test_saving_keeps_notes_and_other_fields(tmp_path):
    path = tmp_path / "local_settings.json"
    path.write_text(json.dumps({"_說明": "mine", "translation": {"profile_mode": "manual", "_n": 1}}),
                    encoding="utf-8")
    settings_store.save_local_settings({"translation.profile_mode": "auto", "subtitle.alpha": 0.8}, path)
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "_說明": "mine", "translation": {"profile_mode": "auto", "_n": 1}, "subtitle": {"alpha": 0.8}}
    path.write_text("{broken", encoding="utf-8")
    import pytest
    with pytest.raises(ValueError):
        settings_store.save_local_settings({"subtitle.alpha": 0.8}, path)
    assert path.read_text(encoding="utf-8") == "{broken"          # never overwritten


def test_example_file_lists_every_tunable_with_its_default():
    from pathlib import Path

    example = json.loads((Path(config.__file__).parent / "local_settings.example.json")
                         .read_text(encoding="utf-8"))
    assert settings_store.settings_problems(example, _Config()) == []
    listed = {k: v for k, v in settings_store.tunable_values(example).items()}
    defaults = settings_store.config_tunables(_Config())
    # every field at its default, except current_activity: owner found it useless, it stays
    # tunable (and dashboard-editable) but is left out of the example
    defaults.pop("translation.current_activity")
    assert listed == defaults


def test_startup_saves_dashboard_edits_and_reports_settings(monkeypatch, tmp_path, caplog):
    import logging

    import main

    path = tmp_path / "local_settings.json"
    path.write_text(json.dumps({"translation": {"profile_mode": "manual"}}), encoding="utf-8")
    monkeypatch.setenv("LIVE_TRANSLATE_LOCAL_SETTINGS", str(path))
    monkeypatch.setattr(settings_store, "PENDING_DASHBOARD_EDITS", {"subtitle.alpha": 0.7})
    monkeypatch.setattr(settings_store, "LOCAL_SETTINGS_PROBLEMS", ["translation.modle：不認得的欄位"])
    monkeypatch.setattr(settings_store, "STALE_DASHBOARD_VALUES", {"subtitle.font_size": 20})
    changed = settings_store._apply_overrides(_Config(), {"subtitle": {"alpha": 0.7}})
    monkeypatch.setattr(main, "cfg", changed)
    with caplog.at_level(logging.INFO):
        main._report_local_settings()
    assert json.loads(path.read_text(encoding="utf-8"))["subtitle"] == {"alpha": 0.7}
    text = caplog.text
    assert "subtitle.alpha = 0.7 (config.py default 1.0)" in text
    assert "modle" in text and "WARNING" in text
    assert "older value subtitle.font_size = 20" in text
    assert "font_size" not in json.loads(path.read_text(encoding="utf-8")).get("subtitle", {})


def test_main_passes_the_profile_lock_to_the_export(monkeypatch):
    import main
    from utils import config_export

    seen = []
    monkeypatch.setattr(config_export, "write", lambda overrides=None: seen.append(overrides))
    args = main.argparse.Namespace(profile="stellive_hina")
    main._export_config_for_dashboard(args)
    main._export_config_for_dashboard(main.argparse.Namespace(profile=None))
    assert seen == [{"streamer_profile": "stellive_hina", "profile_mode": "manual", "use_profile": True}, None]


def test_profile_flag_export_holds_the_locked_profile(monkeypatch, tmp_path):
    from utils import config_export

    monkeypatch.setattr(config_export, "_EXPORT_PATH", tmp_path / "live_translate_config.json")
    config_export.write({"streamer_profile": "stellive_hina", "profile_mode": "manual", "use_profile": True})
    exported = json.loads((tmp_path / "live_translate_config.json").read_text(encoding="utf-8"))
    assert exported["translation"]["streamer_profile"] == "stellive_hina"
    assert exported["translation"]["profile_mode"] == "manual"
    assert "keys" not in exported
    # the snapshot holds exactly what was exported, so it is not mistaken for a dashboard edit
    snapshot = json.loads((tmp_path / "live_translate_config.exported.json").read_text(encoding="utf-8"))
    assert snapshot.pop("_overridden") == ["translation.profile_mode", "translation.streamer_profile",
                                           "translation.use_profile"]
    assert snapshot == settings_store.tunable_values(exported)
    assert settings_store.dashboard_edits(tmp_path / "live_translate_config.json",
                                  tmp_path / "live_translate_config.exported.json") == ({}, {})
