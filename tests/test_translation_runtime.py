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
    _set_last_engine_diagnostics,
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


def _failed_engine(
    name: str,
    *,
    error_type: str | None,
    message_class: str | None,
) -> MagicMock:
    engine = _engine(name, None)

    def fail(*_args):
        _set_last_engine_diagnostics(
            name,
            api_attempt_count=1,
            api_error_type=error_type,
            api_error_message_class=message_class,
        )
        return None

    engine.translate.side_effect = fail
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

    def test_hard_switch_arms_primary_recovery_cooldown(self):
        engines = [
            _failed_engine(
                "primary",
                error_type="timeout",
                message_class="read_timeout",
            ),
            _engine("fallback", "ok"),
        ]
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
            circuit_breaker_enabled=True,
            recovery_cooldown_seconds=60.0,
            clock=lambda: 100.0,
        )

        self.assertEqual((result, used_idx), ("ok", 1))
        self.assertEqual(state.active_idx, 1)
        self.assertEqual(state.primary_cooldown_until, 160.0)
        self.assertEqual(state.consecutive_probe_successes, 0)

    def test_content_rejection_soft_fallback_does_not_open_circuit(self):
        engines = [_engine("primary", "copied source"), _engine("fallback", "ok")]
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
            lambda result, source: result == "copied source",
            logging.getLogger("test"),
            circuit_breaker_enabled=True,
            recovery_cooldown_seconds=60.0,
            clock=lambda: 100.0,
        )

        self.assertEqual((result, used_idx), ("ok", 1))
        self.assertEqual(state.active_idx, 0)
        self.assertEqual(state.consecutive_primary_failures, 0)
        self.assertEqual(state.primary_cooldown_until, 0.0)
        attempts = get_translation_attempts()
        self.assertEqual(attempts[0]["status"], "rejected_output")
        self.assertEqual(attempts[0]["failure_scope"], "content")

    def test_content_rejection_on_intermediate_fallback_does_not_skip_it(self):
        engines = [
            _failed_engine(
                "primary",
                error_type="timeout",
                message_class="read_timeout",
            ),
            _engine("fallback-one", "copied source"),
            _engine("fallback-two", "ok"),
        ]
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
            lambda result, source: result == "copied source",
            logging.getLogger("test"),
            circuit_breaker_enabled=True,
        )

        self.assertEqual((result, used_idx), ("ok", 2))
        self.assertEqual(
            state.active_idx,
            1,
            "Only the primary provider failure is proven; the responsive "
            "content-rejected fallback must remain the next active engine",
        )
        attempts = get_translation_attempts()
        self.assertEqual(
            [attempt["failure_scope"] for attempt in attempts],
            ["provider", "content", "none"],
        )

    def test_contiguous_provider_failures_can_advance_multiple_hops(self):
        engines = [
            _failed_engine(
                "primary",
                error_type="timeout",
                message_class="read_timeout",
            ),
            _failed_engine(
                "fallback-one",
                error_type="connection_error",
                message_class="connection_error",
            ),
            _engine("fallback-two", "ok"),
        ]
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
            circuit_breaker_enabled=True,
        )

        self.assertEqual((result, used_idx), ("ok", 2))
        self.assertEqual(state.active_idx, 2)

    def test_non_circuit_legacy_switches_to_the_successful_fallback(self):
        engines = [
            _engine("primary", None),
            _engine("fallback-one", "copied source"),
            _engine("fallback-two", "ok"),
        ]
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
            lambda result, source: result == "copied source",
            logging.getLogger("test"),
            circuit_breaker_enabled=False,
        )

        self.assertEqual((result, used_idx), ("ok", 2))
        self.assertEqual(state.active_idx, 2)

    def test_unknown_empty_soft_fallback_does_not_open_circuit(self):
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
            circuit_breaker_enabled=True,
        )

        self.assertEqual((result, used_idx), ("ok", 1))
        self.assertEqual(state.active_idx, 0)
        self.assertEqual(state.consecutive_primary_failures, 0)
        self.assertEqual(get_translation_attempts()[0]["failure_scope"], "unknown")

    def test_provider_timeout_opens_circuit(self):
        engines = [
            _failed_engine(
                "primary",
                error_type="timeout",
                message_class="read_timeout",
            ),
            _engine("fallback", "ok"),
        ]
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
            circuit_breaker_enabled=True,
        )

        self.assertEqual((result, used_idx), ("ok", 1))
        self.assertEqual(state.active_idx, 1)
        self.assertEqual(get_translation_attempts()[0]["failure_scope"], "provider")

    def test_empty_content_after_api_attempt_opens_circuit(self):
        engines = [
            _failed_engine(
                "primary",
                error_type=None,
                message_class=None,
            ),
            _engine("fallback", "ok"),
        ]
        state = FallbackState()

        call_with_fallback(
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
            circuit_breaker_enabled=True,
        )

        self.assertEqual(state.active_idx, 1)
        self.assertEqual(get_translation_attempts()[0]["failure_scope"], "provider")

    def test_rate_limit_and_http_5xx_are_provider_failures(self):
        for message_class in ("rate_limit", "http_5xx"):
            with self.subTest(message_class=message_class):
                reset_translation_call_trace()
                engines = [
                    _failed_engine(
                        "primary",
                        error_type="api_error",
                        message_class=message_class,
                    ),
                    _engine("fallback", "ok"),
                ]
                state = FallbackState()

                call_with_fallback(
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
                    circuit_breaker_enabled=True,
                )

                self.assertEqual(state.active_idx, 1)
                self.assertEqual(
                    get_translation_attempts()[0]["failure_scope"],
                    "provider",
                )

    def test_transport_parse_and_explicit_empty_are_provider_failures(self):
        cases = (
            ("connection_error", "connection_error"),
            ("parse_error", "json_parse_error"),
            ("api_error", "empty_response"),
        )
        for error_type, message_class in cases:
            with self.subTest(
                error_type=error_type,
                message_class=message_class,
            ):
                reset_translation_call_trace()
                engines = [
                    _failed_engine(
                        "primary",
                        error_type=error_type,
                        message_class=message_class,
                    ),
                    _engine("fallback", "ok"),
                ]
                state = FallbackState()

                call_with_fallback(
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
                    circuit_breaker_enabled=True,
                )

                self.assertEqual(state.active_idx, 1)
                self.assertEqual(
                    get_translation_attempts()[0]["failure_scope"],
                    "provider",
                )

    def test_auth_and_other_http_4xx_do_not_open_circuit(self):
        for message_class in ("auth_error", "http_4xx"):
            with self.subTest(message_class=message_class):
                reset_translation_call_trace()
                engines = [
                    _failed_engine(
                        "primary",
                        error_type="api_error",
                        message_class=message_class,
                    ),
                    _engine("fallback", "ok"),
                ]
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
                    circuit_breaker_enabled=True,
                )

                self.assertEqual((result, used_idx), ("ok", 1))
                self.assertEqual(state.active_idx, 0)
                self.assertEqual(
                    get_translation_attempts()[0]["failure_scope"],
                    "unknown",
                )

    def test_recovery_probe_skips_primary_during_cooldown(self):
        metrics.reset()
        engines = [_engine("primary", "primary ok"), _engine("fallback", "fallback")]
        state = FallbackState(active_idx=1, primary_cooldown_until=160.0)
        observations = []

        recovered = probe_primary_recovery(
            engines,
            state,
            "probe source",
            "prompt",
            lambda result, source: False,
            logging.getLogger("test"),
            circuit_breaker_enabled=True,
            recovery_cooldown_seconds=60.0,
            required_consecutive_successes=2,
            observation_sink=observations.append,
            clock=lambda: 159.0,
        )

        self.assertFalse(recovered)
        self.assertEqual(state.active_idx, 1)
        engines[0].translate.assert_not_called()
        snapshot = metrics.snapshot()
        self.assertEqual(snapshot.counters["translation.fallback.probe_cooldown_skipped"], 1)
        self.assertNotIn("translation.fallback.probe", snapshot.counters)
        self.assertEqual(observations[0]["status"], "cooldown_skipped")
        self.assertEqual(observations[0]["cooldown_remaining_seconds"], 1.0)

    def test_circuit_breaker_requires_consecutive_successful_probes(self):
        metrics.reset()
        engines = [_engine("primary", "primary ok"), _engine("fallback", "fallback")]
        state = FallbackState(active_idx=1)
        history = [("이전 문장", "先前句子")]
        observations = []
        kwargs = {
            "circuit_breaker_enabled": True,
            "recovery_cooldown_seconds": 60.0,
            "required_consecutive_successes": 2,
            "history": history,
            "observation_sink": observations.append,
            "clock": lambda: 100.0,
        }

        first = probe_primary_recovery(
            engines, state, "probe source", "prompt",
            lambda result, source: False, logging.getLogger("test"), **kwargs,
        )
        self.assertFalse(first)
        self.assertEqual(state.active_idx, 1)
        self.assertEqual(state.consecutive_probe_successes, 1)

        second = probe_primary_recovery(
            engines, state, "probe source", "prompt",
            lambda result, source: False, logging.getLogger("test"), **kwargs,
        )
        self.assertTrue(second)
        self.assertEqual(state.active_idx, 0)
        self.assertEqual(state.consecutive_probe_successes, 0)
        self.assertEqual(engines[0].translate.call_count, 2)
        self.assertEqual(
            engines[0].translate.call_args_list[0].args,
            ("probe source", "prompt", False, history),
        )
        self.assertEqual(
            [(item["status"], item["recovered"], item["success_streak"]) for item in observations],
            [("success", False, 1), ("success", True, 2)],
        )
        snapshot = metrics.snapshot()
        self.assertEqual(snapshot.counters["translation.fallback.probe_success"], 2)
        self.assertEqual(snapshot.counters["translation.fallback.primary_recovered"], 1)

    def test_failed_probe_resets_streak_and_restarts_cooldown(self):
        metrics.reset()
        engines = [_engine("primary", None), _engine("fallback", "fallback")]
        state = FallbackState(active_idx=1, consecutive_probe_successes=1)
        observations = []

        recovered = probe_primary_recovery(
            engines,
            state,
            "probe source",
            "prompt",
            lambda result, source: False,
            logging.getLogger("test"),
            circuit_breaker_enabled=True,
            recovery_cooldown_seconds=60.0,
            required_consecutive_successes=2,
            observation_sink=observations.append,
            clock=lambda: 100.0,
        )

        self.assertFalse(recovered)
        self.assertEqual(state.active_idx, 1)
        self.assertEqual(state.consecutive_probe_successes, 0)
        self.assertEqual(state.primary_cooldown_until, 160.0)
        self.assertEqual(observations[0]["status"], "empty")

        probe_primary_recovery(
            engines,
            state,
            "probe source",
            "prompt",
            lambda result, source: False,
            logging.getLogger("test"),
            circuit_breaker_enabled=True,
            recovery_cooldown_seconds=60.0,
            required_consecutive_successes=2,
            clock=lambda: 120.0,
        )
        self.assertEqual(engines[0].translate.call_count, 1)

    def test_probe_exception_resets_streak_and_restarts_cooldown(self):
        metrics.reset()
        engines = [_engine("primary", None), _engine("fallback", "fallback")]
        engines[0].translate.side_effect = TimeoutError("probe timeout")
        state = FallbackState(active_idx=1, consecutive_probe_successes=1)
        observations = []

        recovered = probe_primary_recovery(
            engines,
            state,
            "probe source",
            "prompt",
            lambda result, source: False,
            logging.getLogger("test"),
            circuit_breaker_enabled=True,
            recovery_cooldown_seconds=60.0,
            required_consecutive_successes=2,
            observation_sink=observations.append,
            clock=lambda: 100.0,
        )

        self.assertFalse(recovered)
        self.assertEqual(state.active_idx, 1)
        self.assertEqual(state.consecutive_probe_successes, 0)
        self.assertEqual(state.primary_cooldown_until, 160.0)
        self.assertEqual(metrics.snapshot().counters["translation.fallback.probe_error"], 1)
        self.assertEqual(observations[0]["status"], "exception")
        self.assertEqual(observations[0]["exception_type"], "TimeoutError")

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
