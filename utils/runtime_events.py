from __future__ import annotations

import json
import math
import os
import re
import threading
from datetime import datetime, timezone, tzinfo
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


def _date_from_clock(clock_value: str, filename_timezone: tzinfo | None = None) -> str:
    """Best-effort YYYYMMDD extraction from a clock() return value.

    Accepts ISO-8601 strings (`2026-05-14T...`) and converts aware datetimes to
    the local timezone by default. This keeps runtime_events_YYYYMMDD aligned
    with translations_YYYYMMDD.txt, which is written with local time.
    """
    if isinstance(clock_value, str):
        try:
            parsed = datetime.fromisoformat(clock_value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(filename_timezone)
            return parsed.strftime("%Y%m%d")
        except ValueError:
            pass
    now = datetime.now(filename_timezone) if filename_timezone is not None else datetime.now()
    return now.strftime("%Y%m%d")


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
        filename_timezone: tzinfo | None = None,
    ):
        self._log_dir = Path(log_dir)
        self.run_id = run_id or _default_run_id()
        self._clock = clock
        self._filename_timezone = filename_timezone
        self._lock = threading.Lock()
        self._warned = False

    @property
    def path(self) -> Path:
        day = _date_from_clock(self._clock(), self._filename_timezone)
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


def _is_japanese(char: str) -> bool:
    return "\u3040" <= char <= "\u30ff" or "\u31f0" <= char <= "\u31ff"


# Label/refusal phrasing that should never appear in a zh-TW subtitle: leaked
# prompt scaffolding ("translation:", "\ubc88\uc5ed:") or the model's English refusal
# boilerplate. Deliberately excludes bare CJK apology words (\u62b1\u6b49/\uc8c4\uc1a1) because
# those are legitimate translations of a streamer actually apologising.
_META_LEAK_RE = re.compile(
    r"(?:^|\s)(?:input|output|translation|translate|source|target|\ubc88\uc5ed|\ucd9c\ub825|\uc785\ub825|\u8bd1\u6587|\u539f\u6587)\s*[:\uff1a]"
    r"|\b(?:i\s*(?:can\s*not|cannot|can't|am\s*unable)|i'?m\s*sorry|as\s*an\s*ai"
    r"|i\s*apologize|unable\s*to\s*translate)\b",
    re.IGNORECASE,
)

_BRACKET_PAIRS = {")": "(", "]": "[", "}": "{", "\uff09": "\uff08", "\u3011": "\u3010", "\u300d": "\u300c", "\u300f": "\u300e"}
_BRACKET_OPENERS = frozenset(_BRACKET_PAIRS.values())

# Per-flag deductions from a perfect score of 1.0. Tuned so a single leakage
# flag drops to "warn" and two stacked failures drop to "bad"; empty output is
# an automatic zero. Kept here as the single source of truth for the scalar.
_QUALITY_PENALTIES = {
    "empty_target": 1.0,
    "target_meta_leak": 0.6,
    "low_target_cjk": 0.4,
    "target_has_hangul": 0.4,
    "repetitive_target": 0.4,
    "target_high_latin": 0.3,
    "very_short_target": 0.3,
    "target_has_japanese": 0.25,
    "long_target_ratio": 0.2,
    "unbalanced_brackets": 0.15,
    "low_source_hangul": 0.1,
}


def _distinct_bigram_ratio(text: str) -> float:
    """Distinct character bigrams / total \u2014 a degeneration signal.

    Whitespace is stripped first so the metric works for space-free CJK. A low
    ratio means the text loops on a few characters ("\uc88b\uc544\uc88b\uc544\uc88b\uc544" / "\u7684\u7684\u7684\u7684").
    Returns 1.0 for text too short to have a repeated bigram.
    """
    compact = re.sub(r"\s+", "", text)
    if len(compact) < 2:
        return 1.0
    bigrams = [compact[i : i + 2] for i in range(len(compact) - 1)]
    return len(set(bigrams)) / len(bigrams)


def _brackets_balanced(text: str) -> bool:
    stack: list[str] = []
    for char in text:
        if char in _BRACKET_OPENERS:
            stack.append(char)
        elif char in _BRACKET_PAIRS:
            if not stack or stack[-1] != _BRACKET_PAIRS[char]:
                return False
            stack.pop()
    return not stack


def quality_score(flags: list[str]) -> float:
    """Collapse the qualitative flags into a comparable 0.0\u20131.0 scalar."""
    penalty = sum(_QUALITY_PENALTIES.get(flag, 0.0) for flag in flags)
    return round(max(0.0, 1.0 - penalty), 3)


def quality_severity(score: float) -> str:
    if score >= 0.8:
        return "ok"
    if score >= 0.5:
        return "warn"
    return "bad"


def translation_quality(source_text: str, target_text: str | None) -> dict[str, Any]:
    source = source_text or ""
    target = target_text or ""
    target_len = len(target)
    source_len = len(source)
    source_hangul_ratio = round(_ratio(source, _is_hangul), 3)
    source_latin_ratio = round(_ratio(source, _is_latin), 3)
    target_cjk_ratio = round(_ratio(target, _is_cjk), 3)
    target_hangul_ratio = round(_ratio(target, _is_hangul), 3)
    target_latin_ratio = round(_ratio(target, _is_latin), 3)
    target_japanese_count = sum(1 for char in target if _is_japanese(char))
    len_ratio = round(target_len / max(1, source_len), 3)
    distinct_bigram_ratio = round(_distinct_bigram_ratio(target), 3)

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
    if target_len >= 4 and target_hangul_ratio > 0:
        flags.append("target_has_hangul")
    if target_len >= 8 and (target_latin_ratio >= 0.18 or sum(1 for char in target if _is_latin(char)) >= 8):
        flags.append("target_high_latin")
    if target_japanese_count > 0:
        flags.append("target_has_japanese")
    # Degeneration: long output that loops on a few characters/bigrams.
    if len(re.sub(r"\s+", "", target)) >= 6 and distinct_bigram_ratio < 0.5:
        flags.append("repetitive_target")
    if target and _META_LEAK_RE.search(target):
        flags.append("target_meta_leak")
    if target and not _brackets_balanced(target):
        flags.append("unbalanced_brackets")

    score = quality_score(flags)

    return {
        "source_len": source_len,
        "target_len": target_len,
        "source_hangul_ratio": source_hangul_ratio,
        "source_latin_ratio": source_latin_ratio,
        "target_cjk_ratio": target_cjk_ratio,
        "target_hangul_ratio": target_hangul_ratio,
        "target_latin_ratio": target_latin_ratio,
        "target_japanese_count": target_japanese_count,
        "target_source_len_ratio": len_ratio,
        "target_distinct_bigram_ratio": distinct_bigram_ratio,
        "quality_flags": flags,
        "quality_score": score,
        "quality_severity": quality_severity(score),
    }


runtime_events = RuntimeEventWriter()
