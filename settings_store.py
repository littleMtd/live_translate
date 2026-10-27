"""The owner's settings: local_settings.toml and dashboard edits, applied onto config.py.

config.py holds the defaults; this module holds everything about the settings files:
the whitelist of tunable fields and their validation, reading local_settings.toml,
measuring dashboard edits against the export snapshot, and saving them back.
config.py calls load() once at import; main.py reports and saves the results.
"""
import json
import math
import os
from dataclasses import replace
from pathlib import Path

from modules.streamer_profiles import canonical_profile_id


def _config():
    # config.py imports this module at its end, after every dataclass and constant
    # exists, so a call-time lookup never sees a half-built config module.
    import config

    return config


# LIVE_TRANSLATE_DASHBOARD_CONFIG moves it (tests point it at a temp file)
_DASHBOARD_CONFIG_JSON = Path(os.environ.get("LIVE_TRANSLATE_DASHBOARD_CONFIG")
                              or Path(__file__).parent / "logs" / "live_translate_config.json")
_DASHBOARD_OVERRIDE_ENV = "LIVE_TRANSLATE_APPLY_DASHBOARD_CONFIG"

# Only these dashboard-editable fields (src-frontend ConfigPanel.vue) may override
# config.py. config.py stays the single source of truth for everything else; the JSON
# cannot reach any field outside this whitelist.
# subtitle font is stored as a `font` tuple in config.py but flattened into
# font_family/font_size/font_style by config_export; the override reverses that.
_DASHBOARD_OVERRIDE_FIELDS = {
    "audio": ("device_name", "vad_enabled", "vad_silence_sec", "vad_max_speech_sec"),
    "stt": ("primary_engine",),
    "translation": ("translation_mode", "max_tokens", "target_lang",
                    "current_activity", "streamer_profile", "use_profile", "profile_mode"),
    "scene": ("publish_open_set_activity",),
    "subtitle": ("idle_hide_ms", "alpha", "show_previous_line", "global_hotkeys_enabled",
                 "hotkey_bookmark", "hotkey_pause", "hotkey_history"),
}
_DASHBOARD_OVERRIDE_TOP = ("live_engine",)
_FONT_FIELDS = ("font_family", "font_size", "font_style")


def _is_finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _is_typed_enum(value: object, allowed: set[str]) -> bool:
    """JSON enum validation that rejects non-strings without invoking hashing."""
    return isinstance(value, str) and value in allowed


def _dashboard_value_is_valid(
    section: str,
    name: str,
    value: object,
    base: "_Config",
) -> bool:
    """Validate the JSON values that bypass dataclass type annotations.

    Tauri validates normal UI submissions, but this file is persisted between
    runs and may also be edited manually.  Keep malformed overrides from
    creating a config that only fails after a pipeline thread has started.
    """
    if section == "audio":
        if name == "device_name":
            return isinstance(value, str) and bool(value.strip()) and len(value) <= 200
        if name == "vad_enabled":
            return isinstance(value, bool)
        if name == "vad_silence_sec":
            return _is_finite_number(value) and 0.0 < float(value) <= 5.0
        if name == "vad_max_speech_sec":
            return _is_finite_number(value) and base.audio.vad_min_speech_sec < float(value) <= 30.0
    if section == "stt" and name == "primary_engine":
        return _is_typed_enum(value, {"elevenlabs", "groq"})
    if section == "translation":
        if name == "translation_mode":
            return _is_typed_enum(value, _config()._VALID_TRANSLATION_MODES)
        if name == "max_tokens":
            return isinstance(value, int) and not isinstance(value, bool) and 10 <= value <= 500
        if name == "target_lang":
            return isinstance(value, str) and bool(value.strip()) and len(value) <= 50
        if name == "current_activity":
            return isinstance(value, str) and len(value) <= 200
        if name == "streamer_profile":
            return isinstance(value, str) and canonical_profile_id(value) is not None
        if name == "use_profile":
            return isinstance(value, bool)
        if name == "profile_mode":
            return _is_typed_enum(value, {"auto", "manual"})
    if section == "scene" and name == "publish_open_set_activity":
        return isinstance(value, bool)
    if section == "subtitle":
        if name == "idle_hide_ms":
            return isinstance(value, int) and not isinstance(value, bool) and 1000 <= value <= 120000
        if name == "alpha":
            return _is_finite_number(value) and 0.1 <= float(value) <= 1.0
        if name in ("show_previous_line", "global_hotkeys_enabled"):
            return isinstance(value, bool)
        if name.startswith("hotkey_"):
            return isinstance(value, str) and bool(value.strip()) and len(value) <= 40
    return False


def _dashboard_font_is_valid(sub: dict[str, object], base_font: tuple) -> bool:
    family = sub.get("font_family", base_font[0] if len(base_font) > 0 else "Microsoft JhengHei")
    size = sub.get("font_size", base_font[1] if len(base_font) > 1 else 22)
    style = sub.get("font_style", base_font[2] if len(base_font) > 2 else "bold")
    return (
        isinstance(family, str) and bool(family.strip()) and len(family) <= 100
        and isinstance(size, int) and not isinstance(size, bool) and 8 <= size <= 48
        and isinstance(style, str) and bool(style.strip()) and len(style) <= 50
    )


def _apply_dashboard_overrides(base: "_Config", json_path: Path = _DASHBOARD_CONFIG_JSON) -> "_Config":
    """Merge whitelisted fields from a settings JSON file onto ``base``.

    Any read/parse/validation failure falls back to the unmodified ``base`` so a
    stale or malformed JSON never breaks startup.
    """
    data = _read_json_object(json_path)
    return base if data is None else _apply_overrides(base, data)


def _read_json_object(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _apply_overrides(base: "_Config", data: dict) -> "_Config":
    section_updates: dict[str, object] = {}
    for section, fields_ in _DASHBOARD_OVERRIDE_FIELDS.items():
        sub = data.get(section)
        if not isinstance(sub, dict):
            continue
        if section == "stt" and sub.get("primary_engine") == "sensevoice":
            sub = {**sub, "primary_engine": "elevenlabs"}
        changes = {
            name: sub[name]
            for name in fields_
            if name in sub and _dashboard_value_is_valid(section, name, sub[name], base)
        }
        if section == "subtitle" and any(
            key in sub for key in _FONT_FIELDS
        ) and _dashboard_font_is_valid(sub, base.subtitle.font):
            base_font = base.subtitle.font
            changes["font"] = (
                sub.get("font_family", base_font[0] if len(base_font) > 0 else "Microsoft JhengHei"),
                sub.get("font_size", base_font[1] if len(base_font) > 1 else 22),
                sub.get("font_style", base_font[2] if len(base_font) > 2 else "bold"),
            )
        if changes:
            try:
                section_updates[section] = replace(getattr(base, section), **changes)
            except (TypeError, ValueError):
                return base
    top_changes = {}
    for name in _DASHBOARD_OVERRIDE_TOP:
        value = data.get(name)
        if not isinstance(value, str):
            continue
        normalized = _config()._BACKEND_ALIASES.get(value, value)
        if normalized in _config()._VALID_BACKEND_MODES:
            top_changes[name] = normalized
    if not section_updates and not top_changes:
        return base
    try:
        return replace(base, **section_updates, **top_changes)
    except (TypeError, ValueError):
        return base


# The owner's own choices all live in ONE untracked file, local_settings.toml next to
# this one (local_settings.example.toml lists every field). TOML, not JSON, because
# the owner edits it by hand and wants comments next to the values; tomlkit keeps
# those comments when dashboard edits are saved into it. An older local_settings.json
# is still read when no .toml exists. config.py holds defaults
# only: editing it for a personal choice broke request-byte tests and showed up in
# every diff. Same whitelist and validation as the dashboard; problems are reported
# at startup (main.py), never silently dropped.
#
# The dashboard rewrites logs/live_translate_config.json, which main.py exports at
# every start together with a snapshot of the exported tunable values. A field that
# differs from that snapshot was changed in the dashboard since: it is applied and
# main.py saves it into local_settings.toml, so the file stays the single place.
# A hand edit of local_settings.toml is never undone by an older dashboard file.
# LIVE_TRANSLATE_LOCAL_SETTINGS points elsewhere, and "" disables it (tests do).
_LOCAL_SETTINGS_TOML = Path(__file__).parent / "local_settings.toml"
_LOCAL_SETTINGS_JSON = Path(__file__).parent / "local_settings.json"   # before 2026-10-27
_LOCAL_SETTINGS_ENV = "LIVE_TRANSLATE_LOCAL_SETTINGS"
_EXPORT_SNAPSHOT_JSON = _DASHBOARD_CONFIG_JSON.with_name("live_translate_config.exported.json")


def local_settings_path() -> Path | None:
    value = os.environ.get(_LOCAL_SETTINGS_ENV)
    if value is None:
        if not _LOCAL_SETTINGS_TOML.is_file() and _LOCAL_SETTINGS_JSON.is_file():
            return _LOCAL_SETTINGS_JSON
        return _LOCAL_SETTINGS_TOML
    return Path(value) if value else None


def _read_settings(path: Path) -> dict | None:
    """The settings file as plain values (TOML or JSON by suffix); None when unreadable."""
    if path.suffix.lower() != ".toml":
        return _read_json_object(path)
    import tomllib

    try:
        return tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):   # tomllib.TOMLDecodeError is a ValueError
        return None


def tunable_values(data: object) -> dict[str, object]:
    """Whitelisted values in a settings/dashboard JSON, keyed "section.name"."""
    out: dict[str, object] = {}
    if not isinstance(data, dict):
        return out
    for section, names in _DASHBOARD_OVERRIDE_FIELDS.items():
        sub = data.get(section)
        if not isinstance(sub, dict):
            continue
        for name in names + (_FONT_FIELDS if section == "subtitle" else ()):
            if name in sub:
                out[f"{section}.{name}"] = sub[name]
    for name in _DASHBOARD_OVERRIDE_TOP:
        if name in data:
            out[name] = data[name]
    return out


def config_tunables(conf: "_Config") -> dict[str, object]:
    """The same keys read from a config object (font tuple flattened)."""
    out: dict[str, object] = {}
    for section, names in _DASHBOARD_OVERRIDE_FIELDS.items():
        for name in names:
            out[f"{section}.{name}"] = getattr(getattr(conf, section), name)
    font = tuple(conf.subtitle.font) + (None, None, None)
    out.update({f"subtitle.{name}": font[i] for i, name in enumerate(_FONT_FIELDS)})
    for name in _DASHBOARD_OVERRIDE_TOP:
        out[name] = getattr(conf, name)
    return out


def _nest(values: dict[str, object]) -> dict:
    nested: dict = {}
    for key, value in values.items():
        if "." in key:
            section, name = key.split(".", 1)
            nested.setdefault(section, {})[name] = value
        else:
            nested[key] = value
    return nested


def _merge(base: dict, extra: dict) -> dict:
    merged = {key: (dict(value) if isinstance(value, dict) else value) for key, value in base.items()}
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


SNAPSHOT_HISTORY_KEY = "_history"   # earlier exported values per field, newest last


def dashboard_edits(json_path: Path = _DASHBOARD_CONFIG_JSON,
                    snapshot_path: Path = _EXPORT_SNAPSHOT_JSON,
                    base: "_Config | None" = None) -> tuple[dict[str, object], dict[str, object]]:
    """(edits, stale): tunable fields the dashboard changed since main.py last exported them.

    The dashboard caches the config it loaded and saves the whole set, so a field the
    owner never touched there can come back with an older exported value (after a hand
    edit of local_settings.toml). Such a value is reported as stale instead of applied.
    Invalid values are dropped, so they are never saved into local_settings.toml.
    """
    current = _read_json_object(json_path)
    snapshot = _read_json_object(snapshot_path)
    if current is None or snapshot is None:
        return {}, {}
    base = base or _config()._Config()
    history = snapshot.get(SNAPSHOT_HISTORY_KEY)
    history = history if isinstance(history, dict) else {}
    edits, stale = {}, {}
    for key, value in tunable_values(current).items():
        if key not in snapshot or snapshot[key] == value:
            continue
        section, _, name = key.partition(".")
        valid = (_dashboard_font_is_valid({name: value}, base.subtitle.font) if name in _FONT_FIELDS
                 else _dashboard_value_is_valid(section, name, value, base) if name
                 else isinstance(value, str) and _config()._BACKEND_ALIASES.get(value, value) in _config()._VALID_BACKEND_MODES)
        if not valid:
            continue
        if value in (history.get(key) or []):
            stale[key] = value
        else:
            edits[key] = value
    return edits, stale


def settings_problems(data: dict, base: "_Config") -> list[str]:
    """Unknown or invalid entries in local_settings.toml; keys starting with "_" are notes."""
    problems = []
    for section, sub in data.items():
        if section.startswith("_"):
            continue
        if section in _DASHBOARD_OVERRIDE_TOP:
            if not (isinstance(sub, str) and _config()._BACKEND_ALIASES.get(sub, sub) in _config()._VALID_BACKEND_MODES):
                problems.append(f"{section}：值不合法 {sub!r}")
            continue
        names = _DASHBOARD_OVERRIDE_FIELDS.get(section)
        if names is None:
            problems.append(f"{section}：不認得的區段（只能調 local_settings.example.toml 列出的欄位）")
            continue
        if not isinstance(sub, dict):
            problems.append(f"{section}：應該是一組欄位")
            continue
        for name, value in sub.items():
            if name.startswith("_"):
                continue
            if section == "subtitle" and name in _FONT_FIELDS:
                if not _dashboard_font_is_valid({name: value}, base.subtitle.font):
                    problems.append(f"{section}.{name}：值不合法 {value!r}")
            elif name not in names:
                problems.append(f"{section}.{name}：不認得的欄位")
            elif not _dashboard_value_is_valid(section, name, value, base):
                problems.append(f"{section}.{name}：值不合法 {value!r}")
    return problems


def save_local_settings(values: dict[str, object], path: Path | None = None) -> None:
    """Write "section.name" values into the settings file, keeping its other entries
    (and, for TOML, its comments and layout)."""
    path = path or local_settings_path()
    if path is None or not values:
        return
    if _read_settings(path) is None and path.is_file():
        raise ValueError(f"{path.name} cannot be parsed; not overwriting it")
    if path.suffix.lower() == ".toml":
        import tomlkit

        document = tomlkit.parse(path.read_text(encoding="utf-8-sig")) if path.is_file() else tomlkit.document()
        for key, value in values.items():
            section, _, name = key.rpartition(".")
            if not section:
                document[name] = value
                continue
            if section not in document:
                document[section] = tomlkit.table()
            document[section][name] = value
        text = tomlkit.dumps(document)
    else:
        data = _merge(_read_json_object(path) if path.is_file() else {}, _nest(values))
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


# Filled by load(): main.py reports them at startup.
LOCAL_SETTINGS_PROBLEMS: list[str] = []
PENDING_DASHBOARD_EDITS: dict[str, object] = {}
STALE_DASHBOARD_VALUES: dict[str, object] = {}


def load(base: "_Config") -> "_Config":
    """config.py's defaults with local_settings.toml and pending dashboard edits applied."""
    global PENDING_DASHBOARD_EDITS, STALE_DASHBOARD_VALUES
    LOCAL_SETTINGS_PROBLEMS.clear()
    path = local_settings_path()
    if path is None:
        if os.environ.get(_DASHBOARD_OVERRIDE_ENV):
            # local settings disabled: the old whole-file dashboard override (kept for the
            # Tauri launch without a local file setup)
            return _apply_dashboard_overrides(base)
        return base
    data = _read_settings(path) if path.is_file() else {}
    if data is None:
        LOCAL_SETTINGS_PROBLEMS.append(f"{path.name} 格式錯誤，整個檔案沒有套用")
        data = {}
    LOCAL_SETTINGS_PROBLEMS.extend(settings_problems(data, base))
    PENDING_DASHBOARD_EDITS, STALE_DASHBOARD_VALUES = dashboard_edits(base=base)
    return _apply_overrides(base, _merge(data, _nest(PENDING_DASHBOARD_EDITS)))
