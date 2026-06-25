"""Guard the Python -> Tauri dashboard config contract.

utils/config_export.py dumps cfg to logs/live_translate_config.json, which the Rust
ConfigDto (src-tauri/src/state.rs) deserializes. The Rust side is resilient to drift
(serde(default) + unknown fields ignored), so the remaining risk is a UI-rendered
field silently disappearing from the export. This test fails loudly if that happens,
instead of the dashboard quietly showing defaults.

History: the Rust DTO once required translation.gemini_model / evolve_enabled /
evolve_every, which config.py had already removed, so get_config failed at runtime.
"""
from utils import config_export


def _export() -> dict:
    return config_export._to_dict()


def test_top_level_sections_present():
    d = _export()
    for section in ("audio", "stt", "splitter", "translation", "subtitle",
                    "database", "live_engine", "clip_engine", "ollama", "nvidia"):
        assert section in d, f"export missing top-level section: {section}"


def test_ui_rendered_fields_present():
    # Fields the dashboard actually edits/displays (ConfigPanel / SystemStats /
    # CacheStats). If config.py renames or drops one, the panel breaks.
    d = _export()
    assert d["stt"]["primary_engine"] in ("sensevoice", "groq")
    assert isinstance(d["translation"]["engine_chain"], list)
    for field in ("max_tokens", "temperature", "target_lang"):
        assert field in d["translation"], f"translation.{field} missing from export"
    for field in ("font_family", "font_size", "font_style", "alpha", "idle_hide_ms"):
        assert field in d["subtitle"], f"subtitle.{field} missing from export"


def test_font_tuple_is_flattened_not_a_sequence():
    # state.rs expects scalar font_family/font_size/font_style, never a `font` tuple.
    d = _export()
    assert "font" not in d["subtitle"]
    assert isinstance(d["subtitle"]["font_family"], str)
    assert isinstance(d["subtitle"]["font_size"], int)


def test_secrets_never_exported():
    d = _export()
    assert "keys" not in d, "API keys must stay in .env, never in the exported JSON"


def test_dropped_translation_fields_absent():
    # Regression: these were removed from config.py; the Rust DTO no longer requires
    # them. Their reappearance would signal an accidental revert.
    d = _export()
    for stale in ("gemini_model", "evolve_enabled", "evolve_every"):
        assert stale not in d["translation"], f"unexpected stale field: {stale}"
