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
    probe_primary_recovery,
)
from modules.translation_engines import (
    _log_token_usage,
    get_last_token_usage,
    get_selected_translation_attempt,
    get_translation_attempts,
    reset_last_token_usage,
    reset_translation_call_trace,
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
        cache[("first", False, "v1", "engine", "model")] = "one"
        cache[("second", False, "v1", "engine", "model")] = "two"

        self.assertEqual(cache_lookup(cache, "first", False, "v1", "engine", "model"), "one")

        self.assertEqual(list(cache.keys())[-1], ("first", False, "v1", "engine", "model"))

    def test_cache_store_evicts_oldest_entry(self):
        cache = OrderedDict()
        cache_store(
            cache,
            "first",
            False,
            "one",
            "v1",
            max_size=1,
            engine_name="engine",
            model_name="model",
        )
        cache_store(
            cache,
            "second",
            False,
            "two",
            "v1",
            max_size=1,
            engine_name="engine",
            model_name="model",
        )

        self.assertNotIn(("first", False, "v1", "engine", "model"), cache)
        self.assertEqual(cache[("second", False, "v1", "engine", "model")], "two")

    def test_cache_keys_are_engine_specific(self):
        cache = OrderedDict()
        cache_store(
            cache,
            "source",
            False,
            "fallback",
            "v1",
            max_size=2,
            engine_name="groq",
            model_name="fallback-model",
        )

        self.assertIsNone(
            cache_lookup(cache, "source", False, "v1", "nvidia", "primary-model")
        )
        self.assertEqual(
            cache_lookup(cache, "source", False, "v1", "groq", "fallback-model"),
            "fallback",
        )


class TestTranslationRuntimeFallback(unittest.TestCase):
    def setUp(self):
        reset_translation_call_trace()

    def test_active_engine_returns_none_for_invalid_state(self):
        self.assertIsNone(active_engine([], 0))
        self.assertIsNone(active_engine([_engine("primary", "ok")], 9))

    def test_fallback_advances_when_primary_fails(self):
        metrics.reset()
        engines = [_engine("primary", None), _engine("fallback", "ok")]
        state = FallbackState()

        result, used_idx = call_with_fallback(
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
        self.assertEqual(used_idx, 1)
        self.assertEqual(state.active_idx, 1)
        self.assertEqual(state.probe_counter, 0)
        snapshot = metrics.snapshot()
        self.assertEqual(snapshot.counters["translation.fallback.attempt"], 2)
        self.assertEqual(snapshot.counters["translation.fallback.success"], 1)

    def test_user_path_does_not_probe_primary(self):
        metrics.reset()
        engines = [_engine("primary", "primary ok"), _engine("fallback", "fallback ok")]
        state = FallbackState(active_idx=1, probe_counter=49)

        result, used_idx = call_with_fallback(
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

        self.assertEqual(result, "fallback ok")
        self.assertEqual(used_idx, 1)
        self.assertEqual(state.active_idx, 1)
        engines[0].translate.assert_not_called()
        engines[1].translate.assert_called_once()
        self.assertNotIn("translation.fallback.probe", metrics.snapshot().counters)

    def test_background_probe_restores_primary(self):
        metrics.reset()
        engines = [_engine("primary", "primary ok"), _engine("fallback", "fallback ok")]
        state = FallbackState(active_idx=1)

        recovered = probe_primary_recovery(
            engines,
            state,
            "probe source",
            "prompt",
            lambda result, source: False,
            logging.getLogger("test"),
        )

        self.assertTrue(recovered)
        self.assertEqual(state.active_idx, 0)
        engines[0].translate.assert_called_once_with("probe source", "prompt", False, [])
        engines[1].translate.assert_not_called()
        snapshot = metrics.snapshot()
        self.assertEqual(snapshot.counters["translation.fallback.probe"], 1)
        self.assertEqual(snapshot.counters["translation.fallback.primary_recovered"], 1)

    def test_soft_fallback_reports_fallback_engine_without_switching(self):
        metrics.reset()
        engines = [_engine("primary", None), _engine("fallback", "ok")]
        state = FallbackState()

        result, used_idx = call_with_fallback(
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

        self.assertEqual(result, "ok")
        self.assertEqual(used_idx, 1)
        self.assertEqual(state.active_idx, 0)
        self.assertEqual(state.consecutive_primary_failures, 1)

        attempts = get_translation_attempts()
        self.assertEqual(
            [(item["engine"], item["status"], item["selected_for_output"]) for item in attempts],
            [("primary", "empty", False), ("fallback", "success", True)],
        )
        self.assertEqual(get_selected_translation_attempt()["engine"], "fallback")

    def test_soft_fallback_clears_primary_token_usage_when_fallback_has_none(self):
        metrics.reset()
        reset_last_token_usage()
        primary = _engine("primary", None)
        fallback = _engine("fallback", "ok")

        def _bad_primary(*_args):
            _log_token_usage("primary", {"prompt_tokens": 99, "completion_tokens": 1})
            return "bad"

        primary.translate.side_effect = _bad_primary
        state = FallbackState()

        result, used_idx = call_with_fallback(
            [primary, fallback],
            state,
            "source",
            "prompt",
            False,
            [],
            50,
            3,
            lambda result, source: result == "bad",
            logging.getLogger("test"),
        )

        self.assertEqual(result, "ok")
        self.assertEqual(used_idx, 1)
        self.assertEqual(get_last_token_usage(), {})
        attempts = get_translation_attempts()
        self.assertEqual(attempts[0]["token_prompt"], 99)
        self.assertNotIn("token_prompt", attempts[1])

    def test_all_engines_failed_returns_primary_idx(self):
        metrics.reset()
        engines = [_engine("primary", None), _engine("fallback", None)]
        state = FallbackState()

        result, used_idx = call_with_fallback(
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

        self.assertIsNone(result)
        self.assertEqual(used_idx, 0)


if __name__ == "__main__":
    unittest.main()
