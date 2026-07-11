from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Any

from utils.logger import get_logger


log = get_logger("runtime_events")

_DEFAULT_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"

# Version 3 adds run-kind and Git provenance. The fields are additive, while the
# version lets offline tooling distinguish records written before provenance was
# available.
_SCHEMA_VERSION = 3
_RUN_KINDS = frozenset({"live", "test", "replay", "benchmark"})

# Types that are safe to pass straight to json.dumps without coercion.
_JSON_NATIVE_TYPES = (str, bool, int, float, type(None))


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{os.getpid()}"


def _default_run_kind() -> str:
    requested = os.getenv("LIVE_TRANSLATE_RUN_KIND", "").strip().lower()
    if requested in _RUN_KINDS:
        return requested
    if "pytest" in sys.modules or os.getenv("PYTEST_CURRENT_TEST"):
        return "test"
    return "live"


_GIT_PROVENANCE_LOCK = threading.Lock()
_GIT_PROVENANCE: tuple[str, bool | None] | None = None


def _git_provenance() -> tuple[str, bool | None]:
    """Resolve repository state once; event emission must never wait on Git."""
    global _GIT_PROVENANCE
    with _GIT_PROVENANCE_LOCK:
        if _GIT_PROVENANCE is not None:
            return _GIT_PROVENANCE
        root = Path(__file__).resolve().parents[1]
        try:
            sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
                timeout=2,
            ).stdout.strip()
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
                timeout=2,
            ).stdout
            _GIT_PROVENANCE = (sha, bool(status.strip()))
        except (OSError, subprocess.SubprocessError):
            _GIT_PROVENANCE = ("", None)
        return _GIT_PROVENANCE


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
        run_kind: str | None = None,
        git_sha: str | None = None,
        git_dirty: bool | None = None,
    ):
        self._log_dir = Path(log_dir)
        self.run_id = run_id or _default_run_id()
        self._clock = clock
        self._filename_timezone = filename_timezone
        resolved_kind = (run_kind or _default_run_kind()).strip().lower()
        if resolved_kind not in _RUN_KINDS:
            raise ValueError(f"run_kind must be one of {sorted(_RUN_KINDS)}")
        detected_sha, detected_dirty = _git_provenance()
        self.run_kind = resolved_kind
        self.git_sha = detected_sha if git_sha is None else str(git_sha)
        self.git_dirty = detected_dirty if git_dirty is None else bool(git_dirty)
        self._lock = threading.Lock()
        self._warned = False

    @property
    def path(self) -> Path:
        day = _date_from_clock(self._clock(), self._filename_timezone)
        return self._log_dir / f"runtime_events_{day}.jsonl"

    def emit(self, event_type: str, **fields: Any) -> None:
        normalized_fields = {key: _normalize_value(value) for key, value in fields.items()}
        record = {
            "schema_version": _SCHEMA_VERSION,
            "event_type": event_type,
            "run_id": self.run_id,
            "created_at": self._clock(),
            **normalized_fields,
            "run_kind": self.run_kind,
            "git_sha": self.git_sha,
            "git_dirty": self.git_dirty,
        }
        try:
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            # Normalization should make this unreachable, but stay defensive
            # so a malformed field never silently drops the whole event.
            fallback = {
                "schema_version": _SCHEMA_VERSION,
                "event_type": event_type,
                "run_id": self.run_id,
                "created_at": self._clock(),
                "run_kind": self.run_kind,
                "git_sha": self.git_sha,
                "git_dirty": self.git_dirty,
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


_KO_DIGITS = {
    "영": 0,
    "공": 0,
    "일": 1,
    "이": 2,
    "삼": 3,
    "사": 4,
    "오": 5,
    "육": 6,
    "륙": 6,
    "칠": 7,
    "팔": 8,
    "구": 9,
}
_ZH_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "兩": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_SMALL_UNITS_KO = {"십": 10, "백": 100, "천": 1000}
_SMALL_UNITS_ZH = {"十": 10, "百": 100, "千": 1000}
_LARGE_UNITS_KO = {"만": 10000, "억": 100000000}
_LARGE_UNITS_ZH = {"萬": 10000, "万": 10000, "億": 100000000, "亿": 100000000}
_MIN_RELIABLE_AMOUNT = 100
_KO_AMOUNT_RE = re.compile(
    r"(?<![가-힣])"
    r"((?:(?:[0-9０-９,\.영공일이삼사오육륙칠팔구]*[만억])\s*)?"
    r"[0-9０-９,\.영공일이삼사오육륙칠팔구십백천만억]+)"
    r"\s*원"
    r"(?=$|[^가-힣]|[은는이가을를에도만입])"
)
_ZH_AMOUNT_RE = re.compile(
    r"([0-9０-９零〇一二兩两三四五六七八九十百千萬万億亿]"
    r"[0-9０-９,\.\s零〇一二兩两三四五六七八九十百千萬万億亿]*)"
    r"(?:\s*(?:元|塊|块|韓元|韩元|台幣|台币|NTD|TWD|HKD|KRW))?"
)


def _normalize_ascii_number(text: str) -> str:
    return text.translate(str.maketrans("０１２３４５６７８９", "0123456789"))


def _parse_unit_number(
    text: str,
    *,
    digits: dict[str, int],
    small_units: dict[str, int],
    large_units: dict[str, int],
) -> int | None:
    compact = _normalize_ascii_number(text).replace(",", "")
    compact = re.sub(r"\s+", "", compact)
    if not compact:
        return None

    result = 0.0
    section = 0.0
    current: float | None = None
    last_large_unit = 0
    saw_number = False
    index = 0

    while index < len(compact):
        number_match = re.match(r"\d+(?:\.\d+)?", compact[index:])
        if number_match:
            current = float(number_match.group(0))
            saw_number = True
            index += len(number_match.group(0))
            continue

        char = compact[index]
        if char in digits:
            current = float(digits[char])
            saw_number = True
        elif char in small_units:
            section += (current if current is not None else 1.0) * small_units[char]
            current = None
            saw_number = True
        elif char in large_units:
            if current is not None:
                section += current
            if section == 0:
                section = 1.0
            unit = large_units[char]
            result += section * unit
            last_large_unit = unit
            section = 0.0
            current = None
            saw_number = True
        else:
            return None
        index += 1

    if current is not None:
        # Colloquial money shorthand: 一萬五 / 1萬5 means 15,000, not 10,005.
        if result > 0 and section == 0 and 0 < current < 10 and last_large_unit >= 10000:
            section += current * (last_large_unit / 10)
        else:
            section += current

    value = result + section
    if not saw_number or value <= 0:
        return None
    return int(round(value))


def _source_amount_values(text: str) -> list[int]:
    values: list[int] = []
    for match in _KO_AMOUNT_RE.finditer(text or ""):
        token = match.group(1)
        compact = _normalize_ascii_number(token).replace(",", "")
        compact = re.sub(r"\s+", "", compact)
        has_arabic_digit = bool(re.search(r"\d", compact))
        has_amount_unit = any(unit in compact for unit in (*_SMALL_UNITS_KO, *_LARGE_UNITS_KO))
        if not (has_arabic_digit or has_amount_unit):
            continue
        value = _parse_unit_number(
            token,
            digits=_KO_DIGITS,
            small_units=_SMALL_UNITS_KO,
            large_units=_LARGE_UNITS_KO,
        )
        if value is not None and value >= _MIN_RELIABLE_AMOUNT:
            values.append(value)
    return values


def _target_amount_values(text: str) -> list[int]:
    values: list[int] = []
    for match in _ZH_AMOUNT_RE.finditer(text or ""):
        token = match.group(1)
        normalized = _normalize_ascii_number(token)
        # Without a currency marker, require a magnitude unit or a 3+ digit
        # number. This catches "五千" / "15000" while ignoring "一個".
        has_marker = bool(match.group(0)[len(match.group(1)):].strip())
        has_magnitude = any(unit in normalized for unit in (*_SMALL_UNITS_ZH, *_LARGE_UNITS_ZH))
        has_large_digits = any(
            float(number.replace(",", "")) >= 100
            for number in re.findall(r"\d+(?:\.\d+)?", normalized.replace(",", ""))
        )
        if not (has_marker or has_magnitude or has_large_digits):
            continue
        value = _parse_unit_number(
            token,
            digits=_ZH_DIGITS,
            small_units=_SMALL_UNITS_ZH,
            large_units=_LARGE_UNITS_ZH,
        )
        if value is not None:
            values.append(value)
    return values


def _amounts_match(source_values: list[int], target_values: list[int]) -> bool:
    if not source_values:
        return True
    if not target_values:
        return False
    unique_source_values = list(dict.fromkeys(source_values))
    for source_value in unique_source_values:
        if not any(abs(source_value - target_value) <= 1 for target_value in target_values):
            return False
    return True


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
    source_amount_values = _source_amount_values(source)
    target_amount_values = _target_amount_values(target) if source_amount_values else []
    amount_mismatch_candidate = bool(
        source_amount_values and not _amounts_match(source_amount_values, target_amount_values)
    )

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
        "source_amount_values": source_amount_values,
        "target_amount_values": target_amount_values,
        "amount_mismatch_candidate": amount_mismatch_candidate,
        "quality_flags": flags,
        "quality_score": score,
        "quality_severity": quality_severity(score),
    }


runtime_events = RuntimeEventWriter()
