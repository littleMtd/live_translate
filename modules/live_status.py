"""Small thread-safe live status shared with the subtitle window.

Writers: the STT worker (which engine produced each chunk) and the translation
emitter (each sentence's outcome, and every published subtitle with its source).
Reader: the subtitle window, for the status indicator and the recent-subtitle
history. Nothing here is persisted or sent anywhere.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

# Enough for a bookmark's follow-up window even when short lines arrive quickly.
_HISTORY_MAX = 100
_OUTCOME_MAX = 10
# The indicator says "translation failing" when the last N finished sentences all failed
# within this window, so one rejected line never lights it.
_FAILING_STREAK = 3
_FAILING_WINDOW_SEC = 90.0
# "Groq fallback" shows while the most recent transcribed chunk came from Groq and is recent.
_STT_RECENT_SEC = 30.0


@dataclass(frozen=True)
class SubtitleRecord:
    at: float
    source: str
    target: str


class LiveStatus:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._history: deque[SubtitleRecord] = deque(maxlen=_HISTORY_MAX)
        self._outcomes: deque[tuple[float, bool]] = deque(maxlen=_OUTCOME_MAX)
        self._stt_engine = ""
        self._stt_at: float | None = None

    def record_subtitle(self, source: str, target: str) -> None:
        if not target:
            return
        with self._lock:
            self._history.append(SubtitleRecord(self._clock(), source or "", target))

    def record_translation(self, ok: bool) -> None:
        with self._lock:
            self._outcomes.append((self._clock(), bool(ok)))

    def record_stt_engine(self, engine: str) -> None:
        with self._lock:
            self._stt_engine = str(engine or "")
            self._stt_at = self._clock()

    def recent_subtitles(self, limit: int) -> list[SubtitleRecord]:
        with self._lock:
            return list(self._history)[-max(0, limit):] if limit > 0 else []

    def flags(self, *, profile_unresolved: bool) -> list[str]:
        """Short labels for problems worth noticing now; empty when all is well."""
        now = self._clock()
        with self._lock:
            labels = []
            if profile_unresolved:
                labels.append("general")
            if (
                self._stt_engine == "groq"
                and self._stt_at is not None
                and now - self._stt_at <= _STT_RECENT_SEC
            ):
                labels.append("Groq 辨識")
            recent = list(self._outcomes)[-_FAILING_STREAK:]
            if (
                len(recent) == _FAILING_STREAK
                and not any(ok for _at, ok in recent)
                and now - recent[0][0] <= _FAILING_WINDOW_SEC
            ):
                labels.append("翻譯失敗")
            return labels

    def reset(self) -> None:
        with self._lock:
            self._history.clear()
            self._outcomes.clear()
            self._stt_engine = ""
            self._stt_at = None


live_status = LiveStatus()
