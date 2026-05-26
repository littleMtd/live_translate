import logging
import unittest
from collections import OrderedDict
from unittest.mock import MagicMock

from modules.translation_runtime import (
    FallbackState,
    active_engine,
    cache_lookup,
    cache_store,
    call_with_fallback,
)
from utils.metrics import metrics


def _engine(name: str, result: str | None) -> MagicMock:
    engine = MagicMock()
    engine.engine_name = name
    engine.model_name = f"{name}-model"
    engine.translate.return_value = result
    return engine


class TestTranslationRuntimeCache(unittest.TestCase):
    def test_cache_lookup_moves_hit_to_end(self):
        cache = OrderedDict()
        cache[("first", False, "v1")] = "一"
        cache[("second", False, "v1")] = "二"

        self.assertEqual(cache_lookup(cache, "first", False, "v1"), "一")

        self.assertEqual(list(cache.keys())[-1], ("first", False, "v1"))

    def test_cache_store_evicts_oldest_entry(self):
        cache = OrderedDict()
        cache_store(cache, "first", False, "一", "v1", max_size=1)
        cache_store(cache, "second", False, "二", "v1", max_size=1)

        self.assertNotIn(("first", False, "v1"), cache)
        self.assertEqual(cache[("second", False, "v1")], "二")


class TestTranslationRuntimeFallback(unittest.TestCase):
    def test_active_engine_returns_none_for_invalid_state(self):
        self.assertIsNone(active_engine([], 0))
        self.assertIsNone(active_engine([_engine("primary", "ok")], 9))

    def test_fallback_advances_when_primary_fails(self):
        metrics.reset()
        engines = [_engine("primary", None), _engine("fallback", "ok")]
        state = FallbackState()

        result = call_with_fallback(
            engines,
            state,
            "source",
            "prompt",
            False,
            [],
            50,
            1,
            lambda result, source: False,
            logging.getLogger("test"),
        )

        self.assertEqual(result, "ok")
        self.assertEqual(state.active_idx, 1)
        self.assertEqual(state.probe_counter, 0)
        snapshot = metrics.snapshot()
        self.assertEqual(snapshot.counters["translation.fallback.attempt"], 2)
        self.assertEqual(snapshot.counters["translation.fallback.success"], 1)

    def test_probe_restores_primary(self):
        metrics.reset()
        engines = [_engine("primary", "primary ok"), _engine("fallback", "fallback ok")]
        state = FallbackState(active_idx=1, probe_counter=49)

        result = call_with_fallback(
            engines,
            state,
            "source",
            "prompt",
            False,
            [],
            50,
            3,
            lambda result, source: False,
            logging.getLogger("test"),
        )

        self.assertEqual(result, "primary ok")
        self.assertEqual(state.active_idx, 0)
        engines[1].translate.assert_not_called()
        snapshot = metrics.snapshot()
        self.assertEqual(snapshot.counters["translation.fallback.probe"], 1)
        self.assertEqual(snapshot.counters["translation.fallback.primary_recovered"], 1)


if __name__ == "__main__":
    unittest.main()
