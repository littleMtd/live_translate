from __future__ import annotations

from collections import OrderedDict, deque
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass

from config import cfg
from modules.translation_engines import TranslationEngine
from modules.translation_runtime import CacheKey, cache_key, cache_lookup, cache_store
from utils.metrics import metrics
from utils.runtime_events import translation_quality


DBFactory = Callable[[], object]
HistoryWriter = Callable[[str, str], None]
HistoryCohort = tuple[str, str, int]
_DEFAULT_COHORT: HistoryCohort = ("default", "unknown", 0)


@dataclass(frozen=True)
class MemoryLookup:
    result: str | None
    source: str


class TranslationMemory:
    def __init__(
        self,
        *,
        cache: MutableMapping[CacheKey, str] | None = None,
        recent: deque[tuple[str, str]] | None = None,
        recent_window: int = 30,
        max_cache_size: int = 500,
        max_recent_cohorts: int = 8,
        db_factory: DBFactory,
        history_writer: HistoryWriter,
    ):
        self.cache: MutableMapping[CacheKey, str] = cache if cache is not None else OrderedDict()
        initial_recent: deque[tuple[str, str]] = (
            recent if recent is not None else deque(maxlen=max(recent_window, 0))
        )
        self._recent_window = max(recent_window, 0)
        self._max_recent_cohorts = max(max_recent_cohorts, 1)
        self._recent_by_cohort: OrderedDict[
            HistoryCohort, deque[tuple[str, str]]
        ] = OrderedDict()
        if initial_recent:
            self._recent_by_cohort[_DEFAULT_COHORT] = initial_recent
        self._max_cache_size = max_cache_size
        self._db_factory = db_factory
        self._history_writer = history_writer

    @property
    def recent(self) -> deque[tuple[str, str]]:
        return self._cohort_recent(_DEFAULT_COHORT, create=True)

    @recent.setter
    def recent(self, value: deque[tuple[str, str]]) -> None:
        self._recent_by_cohort[_DEFAULT_COHORT] = value

    def context(self, cohort: HistoryCohort | None = None) -> list[tuple[str, str]]:
        if cohort is None and _DEFAULT_COHORT not in self._recent_by_cohort:
            if len(self._recent_by_cohort) == 1:
                key = next(iter(self._recent_by_cohort))
            else:
                key = _DEFAULT_COHORT
        else:
            key = cohort or _DEFAULT_COHORT
        recent = self._recent_by_cohort.get(key)
        if recent is None:
            return []
        self._recent_by_cohort.move_to_end(key)
        return list(recent)

    def cohort_stats(self, cohort: HistoryCohort) -> tuple[int, int]:
        selected = len(self._recent_by_cohort.get(cohort, ()))
        total = sum(len(items) for items in self._recent_by_cohort.values())
        return selected, max(0, total - selected)

    def lookup_existing(
        self,
        text: str,
        incomplete: bool,
        prompt_ver: str,
        active_engine: TranslationEngine | None,
    ) -> str | None:
        return self.lookup_existing_event(text, incomplete, prompt_ver, active_engine).result

    def lookup_existing_event(
        self,
        text: str,
        incomplete: bool,
        prompt_ver: str,
        active_engine: TranslationEngine | None,
    ) -> MemoryLookup:
        cached = self.cache_lookup(text, incomplete, prompt_ver, active_engine)
        if cached:
            metrics.increment("translation.cache.memory_hit")
            self._remember_recent(text, cached, incomplete)
            return MemoryLookup(cached, "memory_hit")

        if incomplete or active_engine is None:
            metrics.increment("translation.cache.skipped")
            return MemoryLookup(None, "skipped")

        db_result = self.db_lookup(text, active_engine, prompt_ver)
        if db_result:
            metrics.increment("translation.cache.db_hit")
            self.cache_store(text, incomplete, db_result, prompt_ver, active_engine)
            self._remember_recent(text, db_result, incomplete)
            return MemoryLookup(db_result, "db_hit")

        metrics.increment("translation.cache.miss")
        return MemoryLookup(None, "miss")

    def record_direct(self, text: str, result: str, incomplete: bool) -> None:
        self._history_writer(text, result)
        self._remember_recent(text, result, incomplete)

    # ── Split memory/I-O variants ─────────────────────────────────────────────
    # The methods below let the caller hold its state lock only for in-memory
    # mutation and perform file/DB I/O outside the lock (review M1): a slow disk
    # or SQLite write must not block the other worker's cache lookups.

    def record_direct_memory(self, text: str, result: str, incomplete: bool,
                             cohort: HistoryCohort | None = None) -> None:
        """In-memory part of record_direct; caller writes history outside the lock."""
        self._remember_recent(text, result, incomplete, cohort)

    def record_recent_context(self, text: str, result: str, incomplete: bool,
                              cohort: HistoryCohort | None = None) -> None:
        """Record ordered conversational context without changing cache/history."""
        self._remember_recent(text, result, incomplete, cohort)

    def record_success_memory(
        self,
        text: str,
        result: str,
        incomplete: bool,
        prompt_ver: str,
        active_engine: TranslationEngine | None = None,
        cohort: HistoryCohort | None = None,
    ) -> None:
        """In-memory part of record_success; caller does history/DB I/O outside."""
        self.cache_store(text, incomplete, result, prompt_ver, active_engine)
        if not incomplete:
            self._remember_recent(text, result, incomplete, cohort)

    def write_history(self, text: str, result: str) -> None:
        self._history_writer(text, result)

    def lookup_memory_event(
        self,
        text: str,
        incomplete: bool,
        prompt_ver: str,
        active_engine: TranslationEngine | None = None,
        *,
        remember_recent: bool = True,
        cohort: HistoryCohort | None = None,
    ) -> MemoryLookup:
        """Cache-only lookup (no DB). Source is "" when undecided — the caller
        is expected to consult the DB outside the lock and call record_db_hit."""
        cached = self.cache_lookup(text, incomplete, prompt_ver, active_engine)
        if cached:
            metrics.increment("translation.cache.memory_hit")
            if remember_recent:
                self._remember_recent(text, cached, incomplete, cohort)
            return MemoryLookup(cached, "memory_hit")
        return MemoryLookup(None, "")

    def record_db_hit(
        self,
        text: str,
        incomplete: bool,
        prompt_ver: str,
        db_result: str,
        active_engine: TranslationEngine | None = None,
        *,
        remember_recent: bool = True,
        cohort: HistoryCohort | None = None,
    ) -> MemoryLookup:
        metrics.increment("translation.cache.db_hit")
        self.cache_store(text, incomplete, db_result, prompt_ver, active_engine)
        if remember_recent:
            self._remember_recent(text, db_result, incomplete, cohort)
        return MemoryLookup(db_result, "db_hit")

    def invalidate_memory(
        self,
        text: str,
        incomplete: bool,
        prompt_ver: str,
        active_engine: TranslationEngine | None = None,
        result: str | None = None,
    ) -> None:
        """In-memory part of invalidate; caller does the DB delete outside."""
        self.cache.pop(self._cache_key(text, incomplete, prompt_ver, active_engine), None)
        self._forget_recent(text, result)

    def invalidate_db(
        self, text: str, active_engine: TranslationEngine, prompt_ver: str
    ) -> None:
        db = self._db_factory()
        delete = getattr(db, "delete", None)
        if callable(delete):
            delete(
                text,
                cfg.translation.target_lang,
                active_engine.engine_name,
                active_engine.model_name,
                prompt_ver,
            )

    def record_success(
        self,
        text: str,
        result: str,
        incomplete: bool,
        prompt_ver: str,
        active_engine: TranslationEngine | None,
    ) -> None:
        self.cache_store(text, incomplete, result, prompt_ver, active_engine)
        self._history_writer(text, result)

        if incomplete:
            return

        self._remember_recent(text, result, incomplete)
        if active_engine is not None:
            self.db_store(text, result, active_engine, prompt_ver)

    def cache_store(
        self,
        text: str,
        incomplete: bool,
        value: str,
        prompt_ver: str,
        active_engine: TranslationEngine | None = None,
    ) -> None:
        engine_name, model_name = self._engine_cache_parts(active_engine)
        cache_store(
            self.cache,
            text,
            incomplete,
            value,
            prompt_ver,
            self._max_cache_size,
            engine_name,
            model_name,
        )

    def cache_lookup(
        self,
        text: str,
        incomplete: bool,
        prompt_ver: str,
        active_engine: TranslationEngine | None = None,
    ) -> str | None:
        engine_name, model_name = self._engine_cache_parts(active_engine)
        return cache_lookup(self.cache, text, incomplete, prompt_ver, engine_name, model_name)

    def invalidate(
        self,
        text: str,
        incomplete: bool,
        prompt_ver: str,
        active_engine: TranslationEngine | None,
        result: str | None = None,
    ) -> None:
        self.cache.pop(self._cache_key(text, incomplete, prompt_ver, active_engine), None)
        self._forget_recent(text, result)
        if incomplete or active_engine is None:
            return
        db = self._db_factory()
        delete = getattr(db, "delete", None)
        if callable(delete):
            delete(
                text,
                cfg.translation.target_lang,
                active_engine.engine_name,
                active_engine.model_name,
                prompt_ver,
            )

    def db_lookup(self, text: str, engine: TranslationEngine, prompt_ver: str) -> str | None:
        return self._db_factory().lookup(
            text,
            cfg.translation.target_lang,
            engine.engine_name,
            engine.model_name,
            prompt_ver,
        )

    def db_store(self, text: str, result: str, engine: TranslationEngine, prompt_ver: str) -> None:
        self._db_factory().store(
            text,
            result,
            cfg.translation.target_lang,
            engine.engine_name,
            engine.model_name,
            prompt_ver,
        )

    @staticmethod
    def _engine_cache_parts(engine: TranslationEngine | None) -> tuple[str, str]:
        if engine is None:
            return "", ""
        return str(engine.engine_name or ""), str(engine.model_name or "")

    def _cache_key(
        self,
        text: str,
        incomplete: bool,
        prompt_ver: str,
        active_engine: TranslationEngine | None,
    ) -> CacheKey:
        engine_name, model_name = self._engine_cache_parts(active_engine)
        return cache_key(text, incomplete, prompt_ver, engine_name, model_name)

    def _remember_recent(self, text: str, result: str, incomplete: bool,
                         cohort: HistoryCohort | None = None) -> None:
        if incomplete:
            return
        severity = translation_quality(text, result).get("quality_severity")
        if severity in ("warn", "bad"):
            metrics.increment("translation.context_gated")
            metrics.increment(f"translation.context_gated.{severity}")
            return
        key = cohort or _DEFAULT_COHORT
        self._forget_recent(text, cohort=key)
        self._cohort_recent(key, create=True).append((text, result))

    def _forget_recent(self, text: str, result: str | None = None,
                       cohort: HistoryCohort | None = None) -> None:
        key = cohort or _DEFAULT_COHORT
        current = self._recent_by_cohort.get(key)
        if current is None:
            return
        self._recent_by_cohort[key] = deque(
            (
                (source, target)
                for source, target in current
                if not (source == text and (result is None or target == result))
            ),
            maxlen=current.maxlen,
        )
        self._recent_by_cohort.move_to_end(key)

    def _cohort_recent(
        self, cohort: HistoryCohort, *, create: bool
    ) -> deque[tuple[str, str]]:
        recent = self._recent_by_cohort.get(cohort)
        if recent is None:
            if not create:
                return deque(maxlen=self._recent_window)
            recent = deque(maxlen=self._recent_window)
            self._recent_by_cohort[cohort] = recent
            while len(self._recent_by_cohort) > self._max_recent_cohorts:
                self._recent_by_cohort.popitem(last=False)
        else:
            self._recent_by_cohort.move_to_end(cohort)
        return recent
