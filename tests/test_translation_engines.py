"""Unit tests for engine-level diagnostics helpers (token usage capture)."""
import unittest
from dataclasses import replace

from modules.translation_engines import (
    effective_system_prompt_for_engine,
    engine_registry,
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
        self.assertIn("Traditional Chinese live subtitle translator", prompt)
        self.assertIn("Korean, English, or Japanese", prompt)
        self.assertIn("Never convert unknown Korean sound-words", prompt)
        self.assertIn("official name or title", prompt)


class TestCompactProfileDigest(unittest.TestCase):
    """The compact prompts must carry the active profile's name digest —
    and only the active profile's."""

    @staticmethod
    def _compact_prompt(engine: str, profile: str, use_profile: bool) -> str:
        from config import cfg

        compact_field = (
            "groq_translation_compact_prompt" if engine == "groq" else "openrouter_compact_prompt"
        )
        original = {
            "compact": getattr(cfg.translation, compact_field),
            "profile": cfg.translation.streamer_profile,
            "use_profile": cfg.translation.use_profile,
        }
        object.__setattr__(cfg.translation, compact_field, True)
        object.__setattr__(cfg.translation, "streamer_profile", profile)
        object.__setattr__(cfg.translation, "use_profile", use_profile)
        try:
            return effective_system_prompt_for_engine(engine, "FULL PRIMARY PROMPT")
        finally:
            object.__setattr__(cfg.translation, compact_field, original["compact"])
            object.__setattr__(cfg.translation, "streamer_profile", original["profile"])
            object.__setattr__(cfg.translation, "use_profile", original["use_profile"])

    def test_digest_lists_profile_and_shared_names(self):
        for engine in ("groq", "openrouter"):
            prompt = self._compact_prompt(engine, "url", use_profile=True)
            self.assertIn("Fixed name renderings", prompt)
            self.assertIn("랑코", prompt)
            self.assertIn("솜먕", prompt)
            self.assertIn("유재석→劉在錫", prompt)  # shared scope rides along
            self.assertIn("유아렐/유아엘=UR:L", prompt)
            self.assertIn("Wish Me Love", prompt)

    def test_wrong_profile_names_do_not_leak(self):
        # 랑코 can't be the probe: it appears in _COMPACT_INVARIANTS itself.
        prompt = self._compact_prompt("groq", "hades_chxxnnx", use_profile=True)
        self.assertIn("Chaenna", prompt)
        self.assertNotIn("솜먕", prompt)
        self.assertNotIn("모카", prompt)

    def test_profile_alias_resolves_to_canonical_digest(self):
        prompt = self._compact_prompt("groq", "hades", use_profile=True)
        self.assertIn("Chaenna", prompt)

    def test_no_digest_without_use_profile(self):
        prompt = self._compact_prompt("groq", "url", use_profile=False)
        self.assertNotIn("Fixed name renderings", prompt)
        self.assertNotIn("솜먕", prompt)

    def test_digest_stays_within_compact_token_budget(self):
        from modules.translation_engines import _compact_profile_digest

        for profile in ("url", "hades_chxxnnx", "mwmeu", "stellive_hina", "isegye_lilpa"):
            digest = _compact_profile_digest(profile)
            # ~4 chars/token upper bound: keep the digest under ~150 tokens.
            self.assertLess(len(digest), 600, f"{profile} digest too large: {len(digest)}")


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


class TestDeepLCacheSignature(unittest.TestCase):
    """DeepL ignores the system prompt, so its cache identity (review P1) must
    come from the settings that actually shape a DeepL response — and must NOT
    follow the LLM prompt text."""

    @staticmethod
    def _signature(**overrides) -> str:
        from config import cfg

        fields = (
            "deepl_target_lang", "deepl_context_window",
            "deepl_history_source_chars", "deepl_history_target_chars",
            "current_activity", "streamer_profile", "use_profile",
        )
        original = {name: getattr(cfg.translation, name) for name in fields}
        try:
            for name, value in overrides.items():
                object.__setattr__(cfg.translation, name, value)
            return effective_system_prompt_for_engine("deepl", "FULL PRIMARY PROMPT")
        finally:
            for name, value in original.items():
                object.__setattr__(cfg.translation, name, value)

    def test_signature_is_not_the_system_prompt(self):
        self.assertNotIn(
            "FULL PRIMARY PROMPT",
            self._signature(),
            "DeepL never sees the LLM prompt; hashing it over-rotates the cache",
        )

    def test_llm_prompt_text_does_not_change_signature(self):
        self.assertEqual(
            effective_system_prompt_for_engine("deepl", "PROMPT A"),
            effective_system_prompt_for_engine("deepl", "PROMPT B"),
        )

    def test_target_lang_changes_signature(self):
        self.assertNotEqual(
            self._signature(deepl_target_lang="ZH-HANT"),
            self._signature(deepl_target_lang="EN"),
        )

    def test_context_budget_changes_signature(self):
        self.assertNotEqual(
            self._signature(deepl_context_window=2),
            self._signature(deepl_context_window=0),
        )

    def test_current_activity_changes_signature(self):
        self.assertNotEqual(
            self._signature(current_activity="StarCraft"),
            self._signature(current_activity="Hades"),
        )

    def test_profile_facts_change_signature(self):
        self.assertNotEqual(
            self._signature(streamer_profile="url", use_profile=True),
            self._signature(streamer_profile="isegye_lilpa", use_profile=True),
        )

    def test_deepl_context_contains_url_core_facts_and_two_history_items(self):
        from config import cfg
        from modules.translation_engines import _deepl_context

        original_profile = cfg.translation.streamer_profile
        original_use = cfg.translation.use_profile
        object.__setattr__(cfg.translation, "streamer_profile", "url")
        object.__setattr__(cfg.translation, "use_profile", True)
        try:
            context, count = _deepl_context([
                ("old", "舊"),
                ("유아렐 신곡", "UR:L新歌"),
                ("URL 링크", "URL網址"),
            ])
        finally:
            object.__setattr__(cfg.translation, "streamer_profile", original_profile)
            object.__setattr__(cfg.translation, "use_profile", original_use)

        self.assertEqual(count, 2)
        self.assertIn("유아렐/유아엘=UR:L", context)
        self.assertIn("Wish Me Love", context)
        self.assertNotIn("Recent subtitle: old", context)


class TestAdaptivePrimaryHistory(unittest.TestCase):
    def test_base_and_dependency_windows(self):
        from config import cfg
        from modules.translation_engines import _limited_primary_history

        history = [(f"source-{index}", f"target-{index}") for index in range(12)]
        fields = (
            "context_window", "adaptive_history_enabled",
            "adaptive_history_base_window", "adaptive_history_dependency_window",
        )
        original = {name: getattr(cfg.translation, name) for name in fields}
        object.__setattr__(cfg.translation, "context_window", 10)
        object.__setattr__(cfg.translation, "adaptive_history_enabled", True)
        object.__setattr__(cfg.translation, "adaptive_history_base_window", 5)
        object.__setattr__(cfg.translation, "adaptive_history_dependency_window", 10)
        try:
            base = _limited_primary_history(history, "오늘 방송 재미있었어")
            dependent = _limited_primary_history(history, "근데 그건 아니야")
            false_prefix = _limited_primary_history(history, "근데기계가 있어")
            object.__setattr__(cfg.translation, "adaptive_history_enabled", False)
            disabled = _limited_primary_history(history, "오늘 방송 재미있었어")
        finally:
            for name, value in original.items():
                object.__setattr__(cfg.translation, name, value)

        self.assertEqual(len(base), 5)
        self.assertEqual(len(dependent), 10)
        self.assertEqual(len(false_prefix), 5)
        self.assertEqual(len(disabled), 10)

    def test_other_engines_keep_prompt_passthrough_semantics(self):
        self.assertEqual(
            effective_system_prompt_for_engine("nvidia", "FULL PRIMARY PROMPT"),
            "FULL PRIMARY PROMPT",
        )


class TestEngineRegistry(unittest.TestCase):
    def test_registry_availability_matches_factory_contract(self):
        from config import cfg

        original_keys = cfg.keys
        empty_keys = replace(
            original_keys,
            anthropic="",
            google_translate="",
            deepl="",
            nvidia="",
            openrouter="",
            groq_fallback="",
        )
        object.__setattr__(cfg, "keys", empty_keys)
        try:
            for name, spec in engine_registry().items():
                with self.subTest(engine=name):
                    self.assertEqual(spec.is_configured(), spec.factory().available)
        finally:
            object.__setattr__(cfg, "keys", original_keys)
