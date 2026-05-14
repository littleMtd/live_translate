"""Export Python config to JSON for Tauri dashboard.

API keys are intentionally excluded — they stay in .env only.
The font tuple is flattened into scalar fields for JSON/Rust compatibility.
Called by main.py on startup so Tauri can read live_translate_config.json.
"""
import json
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


def write() -> None:
    """Write non-secret config to JSON for Tauri dashboard."""
    _EXPORT_PATH.parent.mkdir(exist_ok=True)
    _EXPORT_PATH.write_text(
        json.dumps(_to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


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
