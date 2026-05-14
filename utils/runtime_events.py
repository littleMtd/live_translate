from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from utils.logger import get_logger


log = get_logger("runtime_events")

_DEFAULT_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{os.getpid()}"


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
        day = datetime.now().strftime("%Y%m%d")
        return self._log_dir / f"runtime_events_{day}.jsonl"

    def emit(self, event_type: str, **fields: Any) -> None:
        record = {
            "schema_version": 1,
            "event_type": event_type,
            "run_id": self.run_id,
            "created_at": self._clock(),
            **fields,
        }
        try:
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)
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
    return "\uac00" <= char <= "\ud7af" or "\u1100" <= char <= "\u11ff" or "\u3130" <= char <= "\u318f"


def _is_cjk(char: str) -> bool:
    return "\u4e00" <= char <= "\u9fff"


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
