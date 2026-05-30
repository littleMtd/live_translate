"""Unit tests for engine-level diagnostics helpers (token usage capture)."""
import unittest

from modules.translation_engines import (
    _log_token_usage,
    get_last_token_usage,
    reset_last_token_usage,
)


class TestTokenUsageCapture(unittest.TestCase):
    def setUp(self):
        reset_last_token_usage()

    def test_reset_yields_empty(self):
        self.assertEqual(get_last_token_usage(), {})

    def test_openai_style_usage_captured(self):
        _log_token_usage("Groq", {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17})
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


if __name__ == "__main__":
    unittest.main()
