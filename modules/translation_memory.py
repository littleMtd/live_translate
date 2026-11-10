from __future__ import annotations

from collections import deque
from collections.abc import Callable

from modules.conversation_history import ConversationHistory
from modules.session_context import DEFAULT_HISTORY_COHORT, HistoryCohort


HistoryWriter = Callable[[str, str], None]
_DEFAULT_COHORT = DEFAULT_HISTORY_COHORT


class TranslationMemory:
    """Conversation history plus the translations_YYYYMMDD.txt writer.

    The session/SQLite translation cache that used to live here was removed on
    2026-11-10: its key includes the history-dependent prompt version, so it
    had no hit in live runs since September.
    """

    def __init__(
        self,
        *,
        recent: deque[tuple[str, str]] | None = None,
        recent_window: int = 30,
        max_recent_cohorts: int = 8,
        conversation_history: ConversationHistory | None = None,
        history_writer: HistoryWriter,
    ):
        initial_recent: deque[tuple[str, str]] = (
            recent if recent is not None else deque(maxlen=max(recent_window, 0))
        )
        self.history = conversation_history or ConversationHistory(
            recent_window=recent_window,
            max_cohorts=max_recent_cohorts,
            initial_recent=initial_recent,
        )
        self._history_writer = history_writer

    @property
    def recent(self) -> deque[tuple[str, str]]:
        return self.history.recent

    @recent.setter
    def recent(self, value: deque[tuple[str, str]]) -> None:
        self.history.recent = value

    @property
    def _recent_by_cohort(self):
        """Compatibility view while callers migrate to ConversationHistory."""
        return self.history._by_cohort

    def context(self, cohort: HistoryCohort | None = None) -> list[tuple[str, str]]:
        return self.history.context(cohort)

    def cohort_stats(self, cohort: HistoryCohort) -> tuple[int, int]:
        return self.history.cohort_stats(cohort)

    def record_direct(self, text: str, result: str, incomplete: bool) -> None:
        self._history_writer(text, result)
        self._remember_recent(text, result, incomplete)

    # ── Split memory/I-O variants ─────────────────────────────────────────────
    # The caller holds its state lock only for in-memory mutation and writes the
    # history file outside the lock (review M1).

    def record_direct_memory(self, text: str, result: str, incomplete: bool,
                             cohort: HistoryCohort | None = None) -> None:
        """In-memory part of record_direct; caller writes history outside the lock."""
        self._remember_recent(text, result, incomplete, cohort)

    def record_recent_context(self, text: str, result: str, incomplete: bool,
                              cohort: HistoryCohort | None = None) -> None:
        """Record ordered conversational context without writing the history file."""
        self._remember_recent(text, result, incomplete, cohort)

    def write_history(self, text: str, result: str) -> None:
        self._history_writer(text, result)

    def _remember_recent(self, text: str, result: str, incomplete: bool,
                         cohort: HistoryCohort | None = None) -> None:
        self.history.remember(text, result, incomplete, cohort)

    def _forget_recent(self, text: str, result: str | None = None,
                       cohort: HistoryCohort | None = None) -> None:
        self.history.forget(text, result, cohort)

    def _cohort_recent(
        self, cohort: HistoryCohort, *, create: bool
    ) -> deque[tuple[str, str]]:
        return self.history._cohort_recent(cohort, create=create)
