"""Unit tests for engine-level diagnostics helpers (token usage capture)."""
import unittest
import json
from dataclasses import replace
from unittest.mock import MagicMock, patch

from modules.translation_engines import (
    effective_engine_chain_names,
    effective_system_prompt_for_engine,
    engine_registry,
    get_last_engine_api_diagnostics,
    _log_token_usage,
    get_last_token_usage,
    get_last_token_usage_engine,
    reset_last_engine_diagnostics,
    reset_last_token_usage,
    build_effective_deepseek_messages,
    DeepSeekTranslationEngine,
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

    def test_token_log_always_includes_request_effective_profile(self):
        with patch("modules.translation_engines.effective_profile_id", return_value="url"):
            with self.assertLogs("translation_engines", level="INFO") as captured:
                _log_token_usage(
                    "DeepSeek",
                    {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
                )
        self.assertIn(
            "DeepSeek tokens | profile=url | prompt=12 | output=5 | total=17",
            captured.output[-1],
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

    def test_deepseek_cache_usage_is_captured(self):
        _log_token_usage(
            "DeepSeek",
            {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
                "prompt_cache_hit_tokens": 80,
                "prompt_cache_miss_tokens": 20,
            },
        )
        usage = get_last_token_usage()
        self.assertEqual(usage["cache_read"], 80)
        self.assertEqual(usage["cache_write"], 20)

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

        compact_field = "groq_translation_compact_prompt" if engine == "groq" else None
        original = {
            "compact": getattr(cfg.translation, compact_field) if compact_field else None,
            "profile": cfg.translation.streamer_profile,
            "use_profile": cfg.translation.use_profile,
        }
        if compact_field:
            object.__setattr__(cfg.translation, compact_field, True)
        object.__setattr__(cfg.translation, "streamer_profile", profile)
        object.__setattr__(cfg.translation, "use_profile", use_profile)
        try:
            return effective_system_prompt_for_engine(engine, "FULL PRIMARY PROMPT")
        finally:
            if compact_field:
                object.__setattr__(cfg.translation, compact_field, original["compact"])
            object.__setattr__(cfg.translation, "streamer_profile", original["profile"])
            object.__setattr__(cfg.translation, "use_profile", original["use_profile"])

    def test_digest_lists_profile_and_shared_names(self):
        prompt = self._compact_prompt("groq", "url", use_profile=True)
        self.assertIn("Fixed name renderings", prompt)
        self.assertIn("랑코", prompt)
        self.assertIn("솜먕", prompt)
        self.assertIn("유재석→劉在錫", prompt)  # shared scope rides along
        self.assertIn("유아렐/유아엘=UR:L", prompt)
        self.assertIn("Wish Me Love", prompt)

    def test_deepseek_uses_dedicated_production_contract(self):
        deepseek = self._compact_prompt("deepseek", "irise", use_profile=True)

        self.assertIn("You translate spoken Korean", deepseek)
        self.assertIn(
            "Do not mechanically translate an obviously malformed STT token",
            deepseek,
        )
        self.assertIn("If the source is genuinely ambiguous", deepseek)
        self.assertIn("Never invent missing clauses", deepseek)
        self.assertIn("ASR-repair permission never applies", deepseek)
        self.assertIn("never invent Chinese or Latin aliases", deepseek)
        self.assertIn("__LT_UNK_n__ and __LT_SEM_n__", deepseek)
        self.assertIn("Use recent history only for conversational continuity", deepseek)
        self.assertIn("For incomplete input", deepseek)
        self.assertIn("Output only Taiwan Traditional Chinese characters", deepseek)
        self.assertIn("never Simplified Chinese", deepseek)
        self.assertIn("silently replace any Simplified character", deepseek)
        self.assertIn("[Active profile facts]", deepseek)
        self.assertIn("키리/KIIRI=KIIRI", deepseek)
        self.assertNotIn("benchmark", deepseek.lower())
        self.assertNotIn("무송부", deepseek)
        self.assertNotIn("무승부", deepseek)
        self.assertNotIn("You translate noisy live-stream subtitles", deepseek)

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
        for engine in ("groq", "deepseek"):
            prompt = self._compact_prompt(engine, "url", use_profile=False)
            self.assertNotIn("Fixed name renderings", prompt)
            self.assertNotIn("솜먕", prompt)

    def test_digest_stays_within_compact_token_budget(self):
        from modules.translation_engines import _compact_profile_digest

        for profile in ("url", "hades_chxxnnx", "mwmeu", "stellive_hina", "isegye_lilpa"):
            digest = _compact_profile_digest(profile)
            # ~4 chars/token upper bound: keep the digest under ~150 tokens.
            self.assertLess(len(digest), 600, f"{profile} digest too large: {len(digest)}")

    def test_activity_capsule_reaches_both_compact_llm_prompts_once(self):
        from config import cfg

        original = cfg.translation.current_activity
        object.__setattr__(
            cfg.translation,
            "current_activity",
            "  StarCraft   ladder  " + ("x" * 100),
        )
        try:
            for engine in ("groq", "deepseek"):
                prompt = self._compact_prompt(engine, "url", use_profile=True)
                self.assertEqual(
                    prompt.count("[Background] Current stream activity:"),
                    1,
                    engine,
                )
                line = next(
                    item
                    for item in prompt.splitlines()
                    if item.startswith("[Background] Current stream activity:")
                )
                self.assertIn("StarCraft ladder", line)
                self.assertLessEqual(
                    len(line.removeprefix("[Background] Current stream activity: ")),
                    80,
                )
                if engine == "groq":
                    self.assertLess(
                        prompt.index("[Background]"),
                        prompt.index("Final check before answering:"),
                    )
        finally:
            object.__setattr__(cfg.translation, "current_activity", original)


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


class TestDeepSeekTranslationAdapter(unittest.TestCase):
    @staticmethod
    def _response(content: str = "翻譯"):
        response = MagicMock()
        response.__enter__ = MagicMock(return_value=response)
        response.__exit__ = MagicMock(return_value=False)
        response.read.return_value = json.dumps(
            {
                "choices": [
                    {"message": {"content": content}, "finish_reason": "stop"}
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 10,
                    "total_tokens": 110,
                    "prompt_cache_hit_tokens": 80,
                    "prompt_cache_miss_tokens": 20,
                },
                "system_fingerprint": "fp-test",
            }
        ).encode()
        return response

    def test_direct_request_contract_and_cost(self):
        from config import cfg

        engine = DeepSeekTranslationEngine()
        engine._api_key = "test-key"
        messages = (("system", "same prompt"), ("user", "input: 안녕"))
        with patch("urllib.request.urlopen", return_value=self._response()) as urlopen:
            result = engine.translate_messages(messages)

        self.assertEqual(result, "翻譯")
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(request.full_url, "https://api.deepseek.com/chat/completions")
        self.assertEqual(headers["authorization"], "Bearer test-key")
        self.assertEqual(payload["model"], "deepseek-v4-flash")
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(
            payload["messages"],
            [
                {"role": "system", "content": "same prompt"},
                {"role": "user", "content": "input: 안녕"},
            ],
        )
        expected = (
            80 * cfg.translation.deepseek_cache_hit_usd_per_million
            + 20 * cfg.translation.deepseek_cache_miss_usd_per_million
            + 10 * cfg.translation.deepseek_output_usd_per_million
        ) / 1_000_000
        self.assertAlmostEqual(
            get_last_engine_api_diagnostics()["api_cost_usd"], expected, places=10
        )
        self.assertEqual(get_last_token_usage()["cache_read"], 80)
        self.assertEqual(get_last_token_usage()["cache_write"], 20)

    def test_deepseek_cost_is_unknown_when_usage_is_missing_or_incomplete(self):
        engine = DeepSeekTranslationEngine()
        self.assertIsNone(engine._cost_usd({}))
        self.assertIsNone(
            engine._cost_usd(
                {"prompt_tokens": 100, "completion_tokens": 10}
            )
        )

    def test_deepseek_is_registered_for_protected_production_route(self):
        self.assertIn("deepseek", engine_registry())

    def test_protected_and_rollback_chains_ignore_dashboard_base_order(self):
        from config import cfg

        original_route = cfg.translation.deepseek_route
        original_mode = cfg.translation.translation_mode
        try:
            object.__setattr__(cfg.translation, "translation_mode", "live")
            object.__setattr__(cfg.translation, "deepseek_route", "primary")
            self.assertEqual(
                effective_engine_chain_names(),
                ("deepseek", "groq"),
            )
            object.__setattr__(cfg.translation, "deepseek_route", "off")
            self.assertEqual(
                effective_engine_chain_names(),
                ("groq",),
            )
        finally:
            object.__setattr__(cfg.translation, "deepseek_route", original_route)
            object.__setattr__(cfg.translation, "translation_mode", original_mode)
