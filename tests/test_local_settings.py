"""Per-machine profile choice lives in an untracked local_settings.json, not config.py edits."""
import json

import config
from config import _Config, _apply_dashboard_overrides, local_settings_path


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
    assert local_settings_path() == config._LOCAL_SETTINGS_JSON
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

    def run(**env):
        base = {k: v for k, v in os.environ.items() if k != "LIVE_TRANSLATE_APPLY_DASHBOARD_CONFIG"}
        out = subprocess.run([sys.executable, "-c", probe], cwd=repo, capture_output=True, text=True,
                             env={**base, **env}, check=True)
        return out.stdout.split()

    assert run(LIVE_TRANSLATE_LOCAL_SETTINGS=str(path)) == ["stellive_hina", "manual", "True"]
    # all-invalid file: nothing applied, nothing reported
    path.write_text(json.dumps({"translation": {"streamer_profile": "nobody"}}), encoding="utf-8")
    assert run(LIVE_TRANSLATE_LOCAL_SETTINGS=str(path))[2] == "False"


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
