"""Unit tests for engine-level diagnostics helpers (token usage capture)."""
import unittest

from modules.translation_engines import (
    effective_system_prompt_for_engine,
    get_last_engine_api_diagnostics,
    _log_token_usage,
    get_last_token_usage,
    get_last_token_usage_engine,
    reset_last_engine_diagnostics,
    reset_last_token_usage,
)


class TestTokenUsageCapture(unittest.TestCase):
    def setUp(self):
        reset_last_engine_diagnostics()
        reset_last_token_usage()

    def test_reset_yields_empty(self):
        self.assertEqual(get_last_token_usage(), {})

    def test_openai_style_usage_captured(self):
        _log_token_usage("Groq", {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17})
        self.assertEqual(get_last_token_usage_engine(), "groq")
        self.assertEqual(
            get_last_token_usage(),
            {"prompt": 12, "output": 5, "total": 17, "cache_read": None, "cache_write": None},
        )

    def test_gemini_style_usage_captured(self):
        class _Usage:
            prompt_token_count = 30
            candidates_token_count = 8
            total_token_count = 38

        _log_token_usage("Gemini", _Usage())
        usage = get_last_token_usage()
        self.assertEqual(usage["prompt"], 30)
        self.assertEqual(usage["output"], 8)
        self.assertEqual(usage["total"], 38)

    def test_anthropic_style_usage_with_cache(self):
        class _Usage:
            input_tokens = 100
            output_tokens = 20
            cache_read_input_tokens = 80
            cache_creation_input_tokens = 0

        _log_token_usage("Claude", _Usage())
        usage = get_last_token_usage()
        self.assertEqual(usage["prompt"], 100)
        self.assertEqual(usage["output"], 20)
        self.assertEqual(usage["cache_read"], 80)
        self.assertEqual(usage["cache_write"], 0)

    def test_missing_usage_is_all_none(self):
        _log_token_usage("Groq", None)
        self.assertEqual(
            get_last_token_usage(),
            {"prompt": None, "output": None, "total": None, "cache_read": None, "cache_write": None},
        )

    def test_get_returns_a_copy(self):
        _log_token_usage("Groq", {"prompt_tokens": 1})
        snapshot = get_last_token_usage()
        snapshot["prompt"] = 999
        self.assertEqual(get_last_token_usage()["prompt"], 1)

    def test_effective_prompt_uses_groq_compact_prompt(self):
        from config import cfg

        original_compact = cfg.translation.groq_translation_compact_prompt
        original_profile = cfg.translation.use_profile
        object.__setattr__(cfg.translation, "groq_translation_compact_prompt", True)
        object.__setattr__(cfg.translation, "use_profile", False)
        try:
            prompt = effective_system_prompt_for_engine("groq", "FULL PRIMARY PROMPT")
        finally:
            object.__setattr__(cfg.translation, "groq_translation_compact_prompt", original_compact)
            object.__setattr__(cfg.translation, "use_profile", original_profile)

        self.assertNotEqual(prompt, "FULL PRIMARY PROMPT")
        self.assertIn("Korean to Traditional Chinese", prompt)


if __name__ == "__main__":
    unittest.main()


class TestGroqRetryExceptionContract(unittest.TestCase):
    """H2: a timeout during the token-limit retry must return None, not raise."""

    def test_retry_timeout_returns_none(self):
        import io
        import socket
        import urllib.error
        from unittest.mock import patch
        from modules.translation_engines import GroqTranslationEngine

        reset_last_engine_diagnostics()
        engine = GroqTranslationEngine.__new__(GroqTranslationEngine)
        engine._api_key = "test-key"
        engine._model = "qwen/qwen3-32b"
        engine._timeout = 1
        engine._max_tokens = 128
        engine._retry_max_tokens = 96
        engine._strip_think = False

        token_limit_error = urllib.error.HTTPError(
            url="https://api.groq.com/openai/v1/chat/completions",
            code=413,
            msg="payload too large",
            hdrs=None,
            fp=io.BytesIO(b'{"error": {"code": "rate_limit_exceeded", "message": "request too large"}}'),
        )
        calls = {"n": 0}

        def _urlopen(req, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise token_limit_error
            raise urllib.error.URLError(socket.timeout("timed out"))

        with patch("urllib.request.urlopen", side_effect=_urlopen):
            result = engine.translate(
                "안녕하세요", "system", False, history=[("안녕", "你好")]
            )

        self.assertIsNone(result, "retry-path timeout must fail soft (return None)")
        self.assertEqual(calls["n"], 2)
        diagnostics = get_last_engine_api_diagnostics()
        self.assertEqual(diagnostics["engine"], "groq")
        self.assertEqual(diagnostics["api_attempt_count"], 2)
        self.assertEqual(diagnostics["retry_count"], 1)
        self.assertEqual(diagnostics["retry_reason"], "token_limit_without_history")
