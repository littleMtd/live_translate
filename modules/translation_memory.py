from __future__ import annotations

from collections import OrderedDict, deque
from collections.abc import Callable, MutableMapping

from config import cfg
from modules.translation_engines import TranslationEngine
from modules.translation_runtime import CacheKey, cache_lookup, cache_store
from utils.metrics import metrics


DBFactory = Callable[[], object]
HistoryWriter = Callable[[str, str], None]


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
        cached = self.cache_lookup(text, incomplete, prompt_ver)
        if cached:
            metrics.increment("translation.cache.memory_hit")
            self._remember_recent(text, cached, incomplete)
            return cached

        if incomplete or active_engine is None:
            metrics.increment("translation.cache.miss")
            return None

        db_result = self.db_lookup(text, active_engine, prompt_ver)
        if db_result:
            metrics.increment("translation.cache.db_hit")
            self.cache_store(text, incomplete, db_result, prompt_ver)
            self._remember_recent(text, db_result, incomplete)
            return db_result

        metrics.increment("translation.cache.miss")
        return None

    def record_direct(self, text: str, result: str, incomplete: bool) -> None:
        self._history_writer(text, result)
        self._remember_recent(text, result, incomplete)

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

        self.recent.append((text, result))
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
        if not incomplete:
            self.recent.append((text, result))
