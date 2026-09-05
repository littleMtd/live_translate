"""Dashboard -> pipeline config round-trip (config._apply_dashboard_overrides).

The Tauri dashboard saves edits to logs/live_translate_config.json. config.py applies
a whitelisted subset of those edits onto cfg when the launcher opts in via
LIVE_TRANSLATE_APPLY_DASHBOARD_CONFIG, so a restarted pipeline actually reflects the
saved settings instead of silently discarding them.
"""
import json

import config as config_mod


def _write(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_whitelisted_fields_override_and_others_are_ignored(tmp_path):
    base = config_mod._Config()
    json_path = tmp_path / "live_translate_config.json"
    _write(json_path, {
        "subtitle": {"font_size": 30, "alpha": 0.5, "idle_hide_ms": 12000,
                     "font_family": "Foo", "bg": "#ffffff"},
        "translation": {"max_tokens": 123, "target_lang": "ja",
                        "engine_chain": ["groq"], "model": "should-be-ignored"},
        "stt": {"primary_engine": "sensevoice"},
        "audio": {"vad_enabled": False, "vad_silence_sec": 1.2, "vad_max_speech_sec": 12.0},
        "scene": {"publish_open_set_activity": True, "vision_model": "should-be-ignored"},
        "live_engine": base.live_engine,
    })

    merged = config_mod._apply_dashboard_overrides(base, json_path)

    # whitelisted fields applied
    assert merged.subtitle.alpha == 0.5
    assert merged.subtitle.idle_hide_ms == 12000
    # flattened font_family/font_size rebuilt into the config.py `font` tuple,
    # font_style preserved from the base (not present in the JSON)
    assert merged.subtitle.font == ("Foo", 30, base.subtitle.font[2])
    assert merged.translation.max_tokens == 123
    assert merged.translation.target_lang == "ja"
    assert merged.translation.engine_chain == ("groq",)  # list coerced back to tuple
    assert merged.stt.primary_engine == "sensevoice"
    assert merged.audio.vad_enabled is False
    assert merged.audio.vad_max_speech_sec == 12.0
    assert merged.scene.publish_open_set_activity is True
    # NON-whitelisted fields in the JSON must NOT leak through
    assert merged.translation.model == base.translation.model
    assert merged.subtitle.bg == base.subtitle.bg
    assert merged.scene.vision_model == base.scene.vision_model


def test_missing_file_returns_base_unchanged(tmp_path):
    base = config_mod._Config()
    merged = config_mod._apply_dashboard_overrides(base, tmp_path / "absent.json")
    assert merged is base


def test_malformed_json_returns_base_unchanged(tmp_path):
    base = config_mod._Config()
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    assert config_mod._apply_dashboard_overrides(base, bad) is base


def test_invalid_override_value_falls_back_to_base(tmp_path):
    # An invalid live_engine must be rejected by _Config.__post_init__ validation and
    # fall back to the unmodified base rather than crashing startup.
    base = config_mod._Config()
    json_path = tmp_path / "c.json"
    _write(json_path, {"live_engine": "not-a-valid-backend-mode"})
    assert config_mod._apply_dashboard_overrides(base, json_path) is base


def test_elevenlabs_is_an_accepted_stt_engine(tmp_path):
    base = config_mod._Config()
    json_path = tmp_path / "elevenlabs.json"
    _write(json_path, {"stt": {"primary_engine": "elevenlabs"}})

    merged = config_mod._apply_dashboard_overrides(base, json_path)

    assert merged.stt.primary_engine == "elevenlabs"


def test_invalid_field_values_are_ignored_without_discarding_valid_overrides(tmp_path):
    base = config_mod._Config()
    json_path = tmp_path / "invalid-fields.json"
    _write(json_path, {
        "audio": {"vad_enabled": "no", "vad_max_speech_sec": 12.0},
        "subtitle": {"alpha": "opaque", "idle_hide_ms": 12000},
        "stt": {"primary_engine": "not-an-engine"},
        "scene": {"publish_open_set_activity": "true"},
    })

    merged = config_mod._apply_dashboard_overrides(base, json_path)

    assert merged.audio.vad_enabled is base.audio.vad_enabled
    assert merged.audio.vad_max_speech_sec == 12.0
    assert merged.subtitle.alpha == base.subtitle.alpha
    assert merged.subtitle.idle_hide_ms == 12000
    assert merged.stt.primary_engine == base.stt.primary_engine
    assert merged.scene.publish_open_set_activity is True


def test_malformed_enum_types_are_ignored_without_crashing_or_losing_valid_fields(tmp_path):
    base = config_mod._Config()
    malformed_values = ([], {}, None, 1, 1.5, True)

    for index, malformed in enumerate(malformed_values):
        json_path = tmp_path / f"malformed-enum-{index}.json"
        _write(json_path, {
            "stt": {"primary_engine": malformed},
            "translation": {
                "translation_mode": malformed,
                "profile_mode": malformed,
            },
            "live_engine": malformed,
            "subtitle": {"idle_hide_ms": 12000},
        })

        merged = config_mod._apply_dashboard_overrides(base, json_path)

        assert merged.stt.primary_engine == base.stt.primary_engine
        assert merged.translation.translation_mode == base.translation.translation_mode
        assert merged.translation.profile_mode == base.translation.profile_mode
        assert merged.live_engine == base.live_engine
        assert merged.subtitle.idle_hide_ms == 12000


def test_duplicate_translation_routes_are_ignored(tmp_path):
    base = config_mod._Config()
    json_path = tmp_path / "duplicate-routes.json"
    _write(json_path, {
        "translation": {"engine_chain": ["groq", "groq"]},
        "subtitle": {"idle_hide_ms": 12000},
    })

    merged = config_mod._apply_dashboard_overrides(base, json_path)

    assert merged.translation.engine_chain == base.translation.engine_chain
    assert merged.subtitle.idle_hide_ms == 12000


def test_dashboard_cannot_disable_or_reorder_protected_deepseek_route(tmp_path):
    from unittest.mock import patch

    import modules.translation_engines as translation_engines

    base = config_mod._Config()
    json_path = tmp_path / "protected-deepseek.json"
    _write(
        json_path,
        {
            "translation": {
                "deepseek_route": "off",
                # This is a valid dashboard override for other backends, but
                # it must not alter the protected ordinary-live route.
                "engine_chain": ["groq"],
            }
        },
    )

    merged = config_mod._apply_dashboard_overrides(base, json_path)

    assert merged.translation.deepseek_route == "primary"
    assert merged.translation.engine_chain == ("groq",)
    with patch.object(translation_engines, "cfg", merged):
        assert translation_engines.effective_engine_chain_names() == (
            "deepseek",
            "openrouter",
            "deepl",
            "groq",
        )


def test_open_set_publication_override_accepts_only_json_booleans(tmp_path):
    base = config_mod._Config()
    json_path = tmp_path / "scene.json"

    for value in ("true", 1, None):
        _write(json_path, {
            "scene": {"publish_open_set_activity": value},
            "subtitle": {"idle_hide_ms": 12000},
        })
        merged = config_mod._apply_dashboard_overrides(base, json_path)
        assert merged.scene.publish_open_set_activity is True
        assert merged.subtitle.idle_hide_ms == 12000

    disabled_base = config_mod._Config(
        scene=config_mod._Scene(publish_open_set_activity=False)
    )
    _write(json_path, {"scene": {"publish_open_set_activity": True}})
    assert config_mod._apply_dashboard_overrides(
        disabled_base, json_path
    ).scene.publish_open_set_activity is True

    _write(json_path, {"scene": {"publish_open_set_activity": False}})
    assert config_mod._apply_dashboard_overrides(
        base, json_path
    ).scene.publish_open_set_activity is False


def test_no_whitelisted_changes_returns_base(tmp_path):
    base = config_mod._Config()
    json_path = tmp_path / "c.json"
    _write(json_path, {"database": {"db_path": "x"}, "nvidia": {"timeout": 5}})
    assert config_mod._apply_dashboard_overrides(base, json_path) is base
