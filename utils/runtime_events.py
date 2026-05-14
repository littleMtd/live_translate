from __future__ import annotations

import json
import math
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from utils.logger import get_logger


log = get_logger("runtime_events")

_DEFAULT_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"

# Types that are safe to pass straight to json.dumps without coercion.
_JSON_NATIVE_TYPES = (str, bool, int, float, type(None))


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{os.getpid()}"


def _date_from_clock(clock_value: str) -> str:
    """Best-effort YYYYMMDD extraction from a clock() return value.

    Accepts ISO-8601 strings (`2026-05-14T...`) and falls back to today's UTC
    date if the value is unparseable. Keeps tests with injected clocks in
    control of which JSONL file events are written to.
    """
    if isinstance(clock_value, str) and len(clock_value) >= 10:
        head = clock_value[:10]
        if head[4] == "-" and head[7] == "-":
            return head.replace("-", "")
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _normalize_value(value: Any) -> Any:
    """Convert a single field value to something json.dumps can handle natively.

    Non-finite floats (NaN/Inf) become None to keep the JSONL line standard-
    compliant. Lists/tuples/sets/dicts recurse; everything else falls back to
    repr() so the record stays loggable but obviously stringified.
    """
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _normalize_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_normalize_value(v) for v in value]
    # numpy scalars expose .item(); use it before falling back to repr().
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return _normalize_value(item())
        except Exception:
            pass
    return repr(value)


class RuntimeEventWriter:
    def __init__(
        self,
        *,
        log_dir: Path = _DEFAULT_LOG_DIR,
        run_id: str | None = None,
        clock=_utc_now_iso,
    ):
        self._log_dir = Path(log_dir)
        self.run_id = run_id or _default_run_id()
        self._clock = clock
        self._lock = threading.Lock()
        self._warned = False

    @property
    def path(self) -> Path:
        day = _date_from_clock(self._clock())
        return self._log_dir / f"runtime_events_{day}.jsonl"

    def emit(self, event_type: str, **fields: Any) -> None:
        normalized_fields = {key: _normalize_value(value) for key, value in fields.items()}
        record = {
            "schema_version": 1,
            "event_type": event_type,
            "run_id": self.run_id,
            "created_at": self._clock(),
            **normalized_fields,
        }
        try:
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            # Normalization should make this unreachable, but stay defensive
            # so a malformed field never silently drops the whole event.
            fallback = {
                "schema_version": 1,
                "event_type": event_type,
                "run_id": self.run_id,
                "created_at": self._clock(),
                "serialization_error": repr(exc),
                "field_names": sorted(fields.keys()),
            }
            line = json.dumps(fallback, ensure_ascii=False, separators=(",", ":"))
            if not self._warned:
                log.warning("Runtime event serialization fell back: %s", exc)
                self._warned = True

        try:
            with self._lock:
                self._log_dir.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except Exception as exc:
            if not self._warned:
                log.warning("Runtime event write failed: %s", exc)
                self._warned = True


def _ratio(text: str, predicate) -> float:
    return sum(1 for char in text if predicate(char)) / len(text) if text else 0.0


def _is_hangul(char: str) -> bool:
    return "가" <= char <= "힯" or "ᄀ" <= char <= "ᇿ" or "㄰" <= char <= "㆏"


def _is_cjk(char: str) -> bool:
    """True for CJK ideographs across the planes encountered in zh-TW output.

    Covers: Unified (U+4E00-U+9FFF), Extension A (U+3400-U+4DBF),
    Compatibility Ideographs (U+F900-U+FAFF), and Extensions B-F plus
    Compatibility Supplement (U+20000-U+2FFFF). Surrogate-pair characters
    iterate as a single code point in modern Python str, so ord() handles
    SMP code points correctly.
    """
    cp = ord(char)
    return (
        0x4E00 <= cp <= 0x9FFF
        or 0x3400 <= cp <= 0x4DBF
        or 0xF900 <= cp <= 0xFAFF
        or 0x20000 <= cp <= 0x2FFFF
    )


def _is_latin(char: str) -> bool:
    return ("A" <= char <= "Z") or ("a" <= char <= "z")


def translation_quality(source_text: str, target_text: str | None) -> dict[str, Any]:
    source = source_text or ""
    target = target_text or ""
    target_len = len(target)
    source_len = len(source)
    source_hangul_ratio = round(_ratio(source, _is_hangul), 3)
    source_latin_ratio = round(_ratio(source, _is_latin), 3)
    target_cjk_ratio = round(_ratio(target, _is_cjk), 3)
    len_ratio = round(target_len / max(1, source_len), 3)

    flags: list[str] = []
    if not target:
        flags.append("empty_target")
    if source_len >= 8 and source_hangul_ratio < 0.35:
        flags.append("low_source_hangul")
    if target_len >= 8 and target_cjk_ratio < 0.45:
        flags.append("low_target_cjk")
    if target_len <= 3 and source_len >= 10:
        flags.append("very_short_target")
    if target_len > 40 and len_ratio >= 1.5:
        flags.append("long_target_ratio")

    return {
        "source_len": source_len,
        "target_len": target_len,
        "source_hangul_ratio": source_hangul_ratio,
        "source_latin_ratio": source_latin_ratio,
        "target_cjk_ratio": target_cjk_ratio,
        "target_source_len_ratio": len_ratio,
        "quality_flags": flags,
    }


runtime_events = RuntimeEventWriter()
