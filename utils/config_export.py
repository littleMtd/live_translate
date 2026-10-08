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


def _to_dict() -> dict:
    d = _to_jsonable(cfg)
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


def write() -> None:
    """Write non-secret config to JSON for Tauri dashboard."""
    _EXPORT_PATH.parent.mkdir(exist_ok=True)
    _cleanup_stale_temps()
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=_EXPORT_PATH.parent,
                                         prefix=f"{_EXPORT_PATH.name}.", suffix=".tmp",
                                         delete=False) as handle:
            temp_path = Path(handle.name)
            handle.write(json.dumps(_to_dict(), indent=2, ensure_ascii=False))
        os.replace(temp_path, _EXPORT_PATH)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


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
