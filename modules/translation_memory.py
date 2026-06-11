from __future__ import annotations

from collections import OrderedDict, deque
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass

from config import cfg
from modules.translation_engines import TranslationEngine
from modules.translation_runtime import CacheKey, cache_lookup, cache_store
from utils.metrics import metrics
from utils.runtime_events import translation_quality


DBFactory = Callable[[], object]
HistoryWriter = Callable[[str, str], None]


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
        db_factory: DBFactory,
        history_writer: HistoryWriter,
    ):
        self.cache: MutableMapping[CacheKey, str] = cache if cache is not None else OrderedDict()
        self.recent: deque[tuple[str, str]] = (
            recent if recent is not None else deque(maxlen=max(recent_window, 0))
        )
        self._max_cache_size = max_cache_size
        self._db_factory = db_factory
        self._history_writer = history_writer

    def context(self) -> list[tuple[str, str]]:
        return list(self.recent)

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
        cached = self.cache_lookup(text, incomplete, prompt_ver)
        if cached:
            metrics.increment("translation.cache.memory_hit")
            self._remember_recent(text, cached, incomplete)
            return MemoryLookup(cached, "memory_hit")

        if incomplete or active_engine is None:
            metrics.increment("translation.cache.miss")
            return MemoryLookup(None, "skipped")

        db_result = self.db_lookup(text, active_engine, prompt_ver)
        if db_result:
            metrics.increment("translation.cache.db_hit")
            self.cache_store(text, incomplete, db_result, prompt_ver)
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

    def record_direct_memory(self, text: str, result: str, incomplete: bool) -> None:
        """In-memory part of record_direct; caller writes history outside the lock."""
        self._remember_recent(text, result, incomplete)

    def record_success_memory(
        self, text: str, result: str, incomplete: bool, prompt_ver: str
    ) -> None:
        """In-memory part of record_success; caller does history/DB I/O outside."""
        self.cache_store(text, incomplete, result, prompt_ver)
        if not incomplete:
            self._remember_recent(text, result, incomplete)

    def write_history(self, text: str, result: str) -> None:
        self._history_writer(text, result)

    def lookup_memory_event(self, text: str, incomplete: bool, prompt_ver: str) -> MemoryLookup:
        """Cache-only lookup (no DB). Source is "" when undecided — the caller
        is expected to consult the DB outside the lock and call record_db_hit."""
        cached = self.cache_lookup(text, incomplete, prompt_ver)
        if cached:
            metrics.increment("translation.cache.memory_hit")
            self._remember_recent(text, cached, incomplete)
            return MemoryLookup(cached, "memory_hit")
        return MemoryLookup(None, "")

    def record_db_hit(
        self, text: str, incomplete: bool, prompt_ver: str, db_result: str
    ) -> MemoryLookup:
        metrics.increment("translation.cache.db_hit")
        self.cache_store(text, incomplete, db_result, prompt_ver)
        self._remember_recent(text, db_result, incomplete)
        return MemoryLookup(db_result, "db_hit")

    def invalidate_memory(
        self, text: str, incomplete: bool, prompt_ver: str, result: str | None = None
    ) -> None:
        """In-memory part of invalidate; caller does the DB delete outside."""
        self.cache.pop((text, incomplete, prompt_ver), None)
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
        self.cache_store(text, incomplete, result, prompt_ver)
        self._history_writer(text, result)

        if incomplete:
            return

        self._remember_recent(text, result, incomplete)
        if active_engine is not None:
            self.db_store(text, result, active_engine, prompt_ver)

    def cache_store(self, text: str, incomplete: bool, value: str, prompt_ver: str) -> None:
        cache_store(
            self.cache,
            text,
            incomplete,
            value,
            prompt_ver,
            self._max_cache_size,
        )

    def cache_lookup(self, text: str, incomplete: bool, prompt_ver: str) -> str | None:
        return cache_lookup(self.cache, text, incomplete, prompt_ver)

    def invalidate(
        self,
        text: str,
        incomplete: bool,
        prompt_ver: str,
        active_engine: TranslationEngine | None,
        result: str | None = None,
    ) -> None:
        self.cache.pop((text, incomplete, prompt_ver), None)
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

    def _remember_recent(self, text: str, result: str, incomplete: bool) -> None:
        if incomplete:
            return
        severity = translation_quality(text, result).get("quality_severity")
        if severity in ("warn", "bad"):
            metrics.increment("translation.context_gated")
            metrics.increment(f"translation.context_gated.{severity}")
            return
        self.recent.append((text, result))

    def _forget_recent(self, text: str, result: str | None = None) -> None:
        self.recent = deque(
            (
                (source, target)
                for source, target in self.recent
                if not (source == text and (result is None or target == result))
            ),
            maxlen=self.recent.maxlen,
        )
