"""Bounded conversational context, independent of translation cache storage."""

from __future__ import annotations

from collections import OrderedDict, deque

from modules.session_context import DEFAULT_HISTORY_COHORT, HistoryCohort
from utils.metrics import metrics
from utils.runtime_events import translation_quality


class ConversationHistory:
    def __init__(
        self,
        *,
        recent_window: int,
        max_cohorts: int,
        initial_recent: deque[tuple[str, str]] | None = None,
    ) -> None:
        self._recent_window = max(recent_window, 0)
        self._max_cohorts = max(max_cohorts, 1)
        self._by_cohort: OrderedDict[
            HistoryCohort, deque[tuple[str, str]]
        ] = OrderedDict()
        if initial_recent:
            self._by_cohort[DEFAULT_HISTORY_COHORT] = initial_recent

    @property
    def recent(self) -> deque[tuple[str, str]]:
        return self._cohort_recent(DEFAULT_HISTORY_COHORT, create=True)

    @recent.setter
    def recent(self, value: deque[tuple[str, str]]) -> None:
        self._by_cohort[DEFAULT_HISTORY_COHORT] = value

    def context(self, cohort: HistoryCohort | None = None) -> list[tuple[str, str]]:
        if cohort is None and DEFAULT_HISTORY_COHORT not in self._by_cohort:
            key = (
                next(iter(self._by_cohort))
                if len(self._by_cohort) == 1
                else DEFAULT_HISTORY_COHORT
            )
        else:
            key = cohort or DEFAULT_HISTORY_COHORT
        recent = self._by_cohort.get(key)
        if recent is None:
            return []
        self._by_cohort.move_to_end(key)
        return list(recent)

    def cohort_stats(self, cohort: HistoryCohort) -> tuple[int, int]:
        selected = len(self._by_cohort.get(cohort, ()))
        total = sum(len(items) for items in self._by_cohort.values())
        return selected, max(0, total - selected)

    def remember(
        self,
        text: str,
        result: str,
        incomplete: bool,
        cohort: HistoryCohort | None = None,
    ) -> None:
        if incomplete:
            return
        severity = translation_quality(text, result).get("quality_severity")
        if severity in ("warn", "bad"):
            metrics.increment("translation.context_gated")
            metrics.increment(f"translation.context_gated.{severity}")
            return
        key = cohort or DEFAULT_HISTORY_COHORT
        self.forget(text, cohort=key)
        self._cohort_recent(key, create=True).append((text, result))

    def forget(
        self,
        text: str,
        result: str | None = None,
        cohort: HistoryCohort | None = None,
    ) -> None:
        key = cohort or DEFAULT_HISTORY_COHORT
        current = self._by_cohort.get(key)
        if current is None:
            return
        self._by_cohort[key] = deque(
            (
                (source, target)
                for source, target in current
                if not (source == text and (result is None or target == result))
            ),
            maxlen=current.maxlen,
        )
        self._by_cohort.move_to_end(key)

    def _cohort_recent(
        self, cohort: HistoryCohort, *, create: bool
    ) -> deque[tuple[str, str]]:
        recent = self._by_cohort.get(cohort)
        if recent is None:
            if not create:
                return deque(maxlen=self._recent_window)
            recent = deque(maxlen=self._recent_window)
            self._by_cohort[cohort] = recent
            while len(self._by_cohort) > self._max_cohorts:
                self._by_cohort.popitem(last=False)
        else:
            self._by_cohort.move_to_end(cohort)
        return recent

\n