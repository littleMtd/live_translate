"""Export Python config to JSON for Tauri dashboard.

API keys are intentionally excluded — they stay in .env only.
The font tuple is flattened into scalar fields for JSON/Rust compatibility.
Called by main.py on startup so Tauri can read live_translate_config.json.
"""
import json
import os
import tempfile
import re
import time
from pathlib import Path
from dataclasses import fields, is_dataclass
from types import MappingProxyType
from typing import Any

from config import cfg

_EXPORT_PATH = Path(__file__).parent.parent / "logs" / "live_translate_config.json"


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {field.name: _to_jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, MappingProxyType):
        return {key: _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, dict):
        return {key: _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    return value


def _to_dict(translation_overrides: dict | None = None) -> dict:
    d = _to_jsonable(cfg)
    if translation_overrides:
        # e.g. `main.py --profile X`: the profile watcher reloads this file, so it must
        # hold the state actually in force, not config.py's default.
        d.setdefault("translation", {}).update(translation_overrides)
    d.pop("keys", None)
    d.pop("thread_join_timeout", None)
    sub = d.get("subtitle", {})
    if "font" in sub:
        font = sub.pop("font")
        sub["font_family"] = font[0] if len(font) > 0 else "Microsoft JhengHei"
        sub["font_size"]   = font[1] if len(font) > 1 else 22
        sub["font_style"]  = font[2] if len(font) > 2 else "bold"
    return d


_STALE_TEMP_SECONDS = 24 * 60 * 60
_OVERRIDDEN_KEY = "_overridden"  # snapshot fields set by main.py --profile for that run only


def _cleanup_stale_temps() -> None:
    """Remove only this export's old crash leftovers; leave recent writers alone.

    NamedTemporaryFile uses an eight-character random token. Windows refuses
    deletion of an open writer's file; cleanup failures must not block export.
    """
    pattern = re.compile(re.escape(_EXPORT_PATH.name) + r"\.[a-z0-9_]{8}\.tmp")
    cutoff = time.time() - _STALE_TEMP_SECONDS
    for candidate in _EXPORT_PATH.parent.glob(f"{_EXPORT_PATH.name}.*.tmp"):
        try:
            if (pattern.fullmatch(candidate.name) and not candidate.is_symlink()
                    and candidate.is_file() and candidate.stat().st_mtime < cutoff):
                candidate.unlink()
        except OSError:
            pass


def _snapshot_path() -> Path:
    return _EXPORT_PATH.with_name("live_translate_config.exported.json")


def _atomic_write(path: Path, text: str) -> None:
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f"{path.name}.", suffix=".tmp",
                                         delete=False) as handle:
            temp_path = Path(handle.name)
            handle.write(text)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def write(translation_overrides: dict | None = None) -> None:
    """Write non-secret config to JSON for Tauri dashboard.

    A snapshot of the exported tunable values goes next to it: at the next start,
    config.py treats a field that differs from the snapshot as a dashboard edit and
    main.py saves it into local_settings.toml.
    """
    import settings_store

    _EXPORT_PATH.parent.mkdir(exist_ok=True)
    _cleanup_stale_temps()
    data = _to_dict(translation_overrides)
    snapshot = settings_store.tunable_values(data)
    try:
        previous = json.loads(_snapshot_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    previous = previous if isinstance(previous, dict) else {}
    old_history = previous.get(settings_store.SNAPSHOT_HISTORY_KEY)
    old_history = old_history if isinstance(old_history, dict) else {}
    was_overridden = previous.get(_OVERRIDDEN_KEY)
    was_overridden = set(was_overridden) if isinstance(was_overridden, list) else set()
    overridden = {f"translation.{name}" for name in (translation_overrides or {})}
    # History holds only the value before the latest change that did NOT come from the
    # dashboard (a hand edit of local_settings.toml): that is exactly what a dashboard
    # still caching the old config could write back. Dashboard edits and --profile
    # overrides (in either direction) never enter it, so normal switching stays free.
    history = {}
    for key, value in snapshot.items():
        if key not in previous or previous[key] == value:
            if key in old_history:
                history[key] = old_history[key]
        elif not ({key} & (set(settings_store.PENDING_DASHBOARD_EDITS) | was_overridden | overridden)):
            history[key] = [previous[key]]
    if history:
        snapshot[settings_store.SNAPSHOT_HISTORY_KEY] = history
    if overridden:
        snapshot[_OVERRIDDEN_KEY] = sorted(overridden)
    try:
        _atomic_write(_EXPORT_PATH, json.dumps(data, indent=2, ensure_ascii=False))
        _atomic_write(_snapshot_path(), json.dumps(snapshot, indent=2, ensure_ascii=False))
    except BaseException:
        # without a matching snapshot nothing can be misread as a dashboard edit
        _snapshot_path().unlink(missing_ok=True)
        raise


def read() -> dict:
    """Read exported config JSON; falls back to current Python config if absent."""
    if _EXPORT_PATH.exists():
        return json.loads(_EXPORT_PATH.read_text(encoding="utf-8"))
    return _to_dict()


if __name__ == "__main__":
    write()
    print(f"Config exported → {_EXPORT_PATH}")
    import pprint
    pprint.pprint(read())
