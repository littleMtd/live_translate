import sys

from contextlib import contextmanager
from unittest.mock import MagicMock, patch
for _mod in ("anthropic", "google", "google.genai"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

import queue
import tempfile
import threading
import time
import unittest
import unittest.mock
import modules.translation_engines as translation_engines_module
import modules.translator as translator_module
from modules.translation_engines import (
    _build_engine_chain, _build_user_message, TranslationEngine, ClaudeEngine, GoogleTranslateEngine,
    GroqTranslationEngine, NvidiaEngine, OpenRouterTranslationEngine,
    get_last_engine_api_diagnostics, get_last_engine_diagnostics,
)
from modules.translation_prompts import (
    _BASE_PROMPT,
    _build_base_prompt,
    get_translation_profile,
    translation_profile_ids,
)
from modules.translator import (
    _apply_source_aware_corrections,
    _looks_like_meta_garbage_output,
    _normalize_source_before_matching,
    _new_translation_memory,
    _token_usage_for_outcome,
    _write_history,
    get_corrections,
    reset_corrections,
    TranslationOutcome,
    Translator,
)
import modules.db as _db_module


class _NoOpDB:
    """No-op DB that keeps unit tests isolated from the on-disk production DB."""
    @property
    def available(self) -> bool:
        return False
    def lookup(self, *a, **kw):
        return None
    def store(self, *a, **kw):
        pass
    def close(self):
        pass


_db_patch = None
_history_patch = None


def setUpModule():
    global _db_patch, _history_patch
    _db_patch = unittest.mock.patch("modules.translator._get_db", return_value=_NoOpDB())
    _db_patch.start()
    _history_patch = unittest.mock.patch("modules.translator._write_history")
    _history_patch.start()


def tearDownModule():
    if _db_patch:
        _db_patch.stop()
    if _history_patch:
        _history_patch.stop()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_engine(name: str, return_value: str = "你好") -> MagicMock:
    """Create a mock TranslationEngine with a given name and default return value."""
    engine = MagicMock(spec=TranslationEngine)
    engine.engine_name = name
    engine.model_name = f"{name}-test-model"
    engine.available = True
    engine.translate.return_value = return_value
    return engine


def _make_translator() -> Translator:
    """Build a Translator backed by mock engines — no real API clients."""
    from modules.translator import _CACHE_MAX_SIZE
    from modules.translation_policy import TranslationPolicy
    from modules.translation_memory import TranslationMemory
    from config import cfg
    t = Translator.__new__(Translator)
    t._active_idx = 0
    t._probe_counter = 0
    t._consecutive_primary_failures = 0
    t._last_input = ""
    t._engines = [_mock_engine(name) for name in ("gemini", "claude")]
    t._policy = TranslationPolicy(slang=cfg.translation.slang, min_translate_chars=2)
    t._memory = TranslationMemory(
        recent_window=3,
        max_cache_size=_CACHE_MAX_SIZE,
        db_factory=lambda: _NoOpDB(),
        history_writer=MagicMock(),
    )
    return t


def _claude_resp(text: str) -> MagicMock:
    r = MagicMock()
    r.content = [MagicMock(text=text)]
    return r


def _sys_prompt(t: "Translator") -> str:
    return _BASE_PROMPT


@contextmanager
def _active_translation_profile(profile_id: str, use_profile: bool = True):
    from config import cfg

    original_profile = cfg.translation.streamer_profile
    original_use_profile = cfg.translation.use_profile
    object.__setattr__(cfg.translation, "streamer_profile", profile_id)
    object.__setattr__(cfg.translation, "use_profile", use_profile)
    try:
        yield
    finally:
        object.__setattr__(cfg.translation, "streamer_profile", original_profile)
        object.__setattr__(cfg.translation, "use_profile", original_use_profile)


# ---------------------------------------------------------------------------
# Correction recording (source normalization / target corrections)
# ---------------------------------------------------------------------------

class TestCorrectionRecording(unittest.TestCase):
    def setUp(self):
        reset_corrections()

    def test_source_normalization_records_rule_before_after(self):
        with _active_translation_profile("stellive_hina"):
            out = _normalize_source_before_matching("해동이 안녕")

        self.assertEqual(out, "해둥이 안녕")
        recs = get_corrections()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["stage"], "source_norm")
        self.assertEqual(recs[0]["before"], "해동이")
        self.assertEqual(recs[0]["after"], "해둥이")

    def test_shared_target_correction_records(self):
        # 어금니 ("molar") present in source -> mistranslated 牙齦 ("gum") fixed.
        out = _apply_source_aware_corrections("어금니 아파요", "牙齦很痛")

        self.assertEqual(out, "臼齒很痛")
        recs = get_corrections()
        self.assertTrue(
            any(r["stage"] == "target_correction" and r["before"] == "牙齦" and r["after"] == "臼齒"
                for r in recs)
        )

    def test_profile_target_correction_records_海洞_rescue(self):
        with _active_translation_profile("stellive_hina"):
            out = _apply_source_aware_corrections("해둥이들 안녕", "海洞們 你好")

        self.assertIn("해둥이們", out)
        recs = get_corrections()
        self.assertTrue(any(r["after"] == "해둥이們" for r in recs))

    def test_no_correction_records_nothing(self):
        out = _apply_source_aware_corrections("일반적인 문장입니다", "這是一個普通的句子")

        self.assertEqual(out, "這是一個普通的句子")
        self.assertEqual(get_corrections(), [])


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

class TestBuildBasePrompt(unittest.TestCase):

    def test_contains_target_lang(self):
        from config import cfg
        self.assertIn(cfg.translation.target_lang, _build_base_prompt())

    def test_contains_all_slang(self):
        from config import cfg
        prompt = _build_base_prompt()
        for ko, zh in cfg.translation.slang.items():
            self.assertIn(ko, prompt)
            self.assertIn(zh, prompt)

    def test_contains_rules(self):
        prompt = _build_base_prompt()
        self.assertIn("Output the translation only", prompt)
        self.assertIn("Preserve As-Is", prompt)
        self.assertIn("STT", prompt)

    def test_live_mode_stt_section(self):
        from config import cfg
        orig = cfg.translation.translation_mode
        object.__setattr__(cfg.translation, "translation_mode", "live")
        try:
            prompt = _build_base_prompt()
            self.assertIn("STT Correction - Live Mode", prompt)
            self.assertNotIn("STT Correction - Clip Mode", prompt)
        finally:
            object.__setattr__(cfg.translation, "translation_mode", orig)

    def test_clip_mode_stt_section(self):
        from config import cfg
        orig = cfg.translation.translation_mode
        object.__setattr__(cfg.translation, "translation_mode", "clip")
        try:
            prompt = _build_base_prompt()
            self.assertIn("STT Correction - Clip Mode", prompt)
            self.assertNotIn("STT Correction - Live Mode", prompt)
        finally:
            object.__setattr__(cfg.translation, "translation_mode", orig)

    def test_get_translation_profile_returns_profile_text(self):
        self.assertTrue(get_translation_profile("hades_chxxnnx"))

    def test_get_translation_profile_unknown_returns_empty(self):
        self.assertEqual(get_translation_profile("unknown"), "")

    def test_get_translation_profile_supports_qwen_profiles(self):
        self.assertTrue(get_translation_profile("hades_chxxnnx", qwen=True))

    def test_translation_profile_ids_match_streamer_registry(self):
        from modules.streamer_profiles import known_profile_ids

        expected = known_profile_ids() - {""}
        self.assertEqual(translation_profile_ids(), expected)
        self.assertEqual(translation_profile_ids(qwen=True), expected)

    def test_translator_uses_translation_profile_helper(self):
        translator = _make_translator()

        with patch("modules.translator.get_translation_profile", return_value="PROFILE TEXT") as helper:
            prompt = translator._build_system_prompt()

        self.assertIn("PROFILE TEXT", prompt)
        helper.assert_called_once()

    def test_translator_skips_profile_helper_when_profiles_disabled(self):
        from config import cfg

        translator = _make_translator()
        orig = cfg.translation.use_profile
        object.__setattr__(cfg.translation, "use_profile", False)
        try:
            with patch("modules.translator.get_translation_profile") as helper:
                prompt = translator._build_system_prompt()
        finally:
            object.__setattr__(cfg.translation, "use_profile", orig)

        self.assertNotIn("PROFILE TEXT", prompt)
        helper.assert_not_called()


class TestBuildUserMessage(unittest.TestCase):

    def test_contains_text(self):
        msg = _build_user_message("안녕하세요", incomplete=False)
        self.assertIn("안녕하세요", msg)
        self.assertNotIn("incomplete", msg)

    def test_incomplete_flag_present(self):
        self.assertIn("incomplete", _build_user_message("게임 하고", incomplete=True))

    def test_complete_no_incomplete_flag(self):
        self.assertNotIn("incomplete", _build_user_message("안녕하세요", incomplete=False))



# ---------------------------------------------------------------------------
# ClaudeEngine unit tests
# ---------------------------------------------------------------------------

class TestClaudeEngine(unittest.TestCase):

    def _make_engine(self, resp_text: str = "你好", side_effect=None) -> ClaudeEngine:
        e = ClaudeEngine.__new__(ClaudeEngine)
        e._client = MagicMock()
        if side_effect:
            e._client.messages.create.side_effect = side_effect
        else:
            e._client.messages.create.return_value = _claude_resp(resp_text)
        return e

    def test_returns_translated_text(self):
        e = self._make_engine("你好")
        self.assertEqual(e.translate("안녕하세요", _sys_prompt(_make_translator()), False), "你好")

    def test_strips_whitespace(self):
        e = self._make_engine("  你好  ")
        self.assertEqual(e.translate("안녕하세요", _sys_prompt(_make_translator()), False), "你好")

    def test_returns_none_on_exception(self):
        e = self._make_engine(side_effect=Exception("API down"))
        self.assertIsNone(e.translate("안녕하세요", _sys_prompt(_make_translator()), False))

    def test_returns_none_when_client_is_none(self):
        e = ClaudeEngine.__new__(ClaudeEngine)
        e._client = None
        self.assertIsNone(e.translate("안녕하세요", "system", False))




# ---------------------------------------------------------------------------
# GoogleTranslateEngine unit tests
# ---------------------------------------------------------------------------

class TestGoogleTranslateEngine(unittest.TestCase):

    def _call(self, resp_text: str = "你好", side_effect=None, text: str = "안녕하세요") -> "str | None":
        import json
        e = GoogleTranslateEngine.__new__(GoogleTranslateEngine)
        e._api_key = "fake-key"
        e._target_lang = "zh-TW"
        if side_effect:
            with patch("urllib.request.urlopen", side_effect=side_effect):
                return e.translate(text, "system", False)
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = json.dumps(
            {"data": {"translations": [{"translatedText": resp_text}]}}
        ).encode()
        with patch("urllib.request.urlopen", return_value=mock_resp):
            return e.translate(text, "system", False)

    def test_returns_translated_text(self):
        self.assertEqual(self._call("你好"), "你好")

    def test_strips_whitespace(self):
        self.assertEqual(self._call("  你好  "), "你好")

    def test_returns_none_on_exception(self):
        self.assertIsNone(self._call(side_effect=Exception("network down")))

    def test_returns_none_when_api_key_empty(self):
        e = GoogleTranslateEngine.__new__(GoogleTranslateEngine)
        e._api_key = ""
        e._target_lang = "zh-TW"
        self.assertIsNone(e.translate("안녕하세요", "system", False))

    def test_ignores_system_prompt_and_incomplete(self):
        # Direct-translation engine — system_prompt / incomplete do not affect output
        self.assertEqual(self._call("你好", text="안녕하세요"), "你好")
        self.assertEqual(self._call("你好", text="안녕하세요"), "你好")


# ---------------------------------------------------------------------------
# GroqTranslationEngine unit tests
# ---------------------------------------------------------------------------

class TestGroqTranslationEngine(unittest.TestCase):

    def _engine(self) -> GroqTranslationEngine:
        e = GroqTranslationEngine.__new__(GroqTranslationEngine)
        e._api_key = "fake-key"
        e._model = "qwen/qwen3-32b"
        e._timeout = 12
        e._max_tokens = 512
        e._retry_max_tokens = 256
        e._strip_think = True
        return e

    def _response(self, content: str):
        import json
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = json.dumps({
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }).encode()
        return mock_resp

    def test_request_includes_headers_required_by_groq_edge(self):
        e = self._engine()

        with patch("urllib.request.urlopen", return_value=self._response("ok")) as urlopen:
            result = e.translate("hello", "system", False)

        self.assertEqual(result, "ok")
        req = urlopen.call_args.args[0]
        headers = {k.lower(): v for k, v in req.header_items()}
        self.assertEqual(headers["accept"], "application/json")
        self.assertEqual(headers["user-agent"], "live_translate/1.0")
        self.assertEqual(headers["authorization"], "Bearer fake-key")

    def test_uses_compact_prompt_by_default(self):
        import json

        e = self._engine()
        long_prompt = "full quality prompt " * 500

        with patch("urllib.request.urlopen", return_value=self._response("ok")) as urlopen:
            result = e.translate("hello", long_prompt, False)

        self.assertEqual(result, "ok")
        req = urlopen.call_args.args[0]
        payload = json.loads(req.data.decode())
        system_message = payload["messages"][0]["content"]
        self.assertIn("Korean to Traditional Chinese live subtitle translator", system_message)
        self.assertNotIn("full quality prompt", system_message)

    def test_strips_closed_and_unclosed_think_blocks(self):
        e = self._engine()

        with patch("urllib.request.urlopen", return_value=self._response("<think>notes</think>你好")):
            self.assertEqual(e.translate("hello", "system", False), "你好")

        with patch("urllib.request.urlopen", return_value=self._response("<think>unfinished notes")):
            self.assertIsNone(e.translate("hello", "system", False))

    def test_limits_history_and_tokens_for_groq_fallback(self):
        import json
        from config import cfg

        e = self._engine()
        history = [
            (f"source-{i}-" + "x" * 240, f"target-{i}-" + "y" * 300)
            for i in range(4)
        ]
        original_window = cfg.translation.groq_translation_context_window

        try:
            object.__setattr__(cfg.translation, "groq_translation_context_window", 2)
            with patch("urllib.request.urlopen", return_value=self._response("ok")) as urlopen:
                result = e.translate("hello", "system", False, history)
        finally:
            object.__setattr__(
                cfg.translation,
                "groq_translation_context_window",
                original_window,
            )

        self.assertEqual(result, "ok")
        req = urlopen.call_args.args[0]
        payload = json.loads(req.data.decode())
        messages = payload["messages"]

        self.assertEqual(payload["max_tokens"], 512)
        self.assertEqual(len(messages), 6)
        self.assertNotIn("source-1-", str(messages))
        self.assertIn("source-2-", messages[1]["content"])
        self.assertIn("source-3-", messages[3]["content"])
        self.assertTrue(messages[-1]["content"].startswith("/no_think\ninput:"))
        self.assertLessEqual(len(messages[1]["content"]), len("input: ") + 163)
        self.assertLessEqual(len(messages[2]["content"]), 223)

    def test_retries_413_token_limit_without_history(self):
        import io
        import json
        import urllib.error
        from config import cfg

        e = self._engine()
        history = [("source", "target")]
        original_window = cfg.translation.groq_translation_context_window
        error = urllib.error.HTTPError(
            url="https://api.groq.com/openai/v1/chat/completions",
            code=413,
            msg="request too large",
            hdrs=None,
            fp=io.BytesIO(b'{"error":{"message":"TPM token limit","code":"rate_limit_exceeded"}}'),
        )

        try:
            object.__setattr__(cfg.translation, "groq_translation_context_window", 1)
            with patch("urllib.request.urlopen", side_effect=[error, self._response("ok")]) as urlopen:
                result = e.translate("hello", "system", False, history)
        finally:
            object.__setattr__(
                cfg.translation,
                "groq_translation_context_window",
                original_window,
            )

        self.assertEqual(result, "ok")
        self.assertEqual(urlopen.call_count, 2)

        first_req = urlopen.call_args_list[0].args[0]
        retry_req = urlopen.call_args_list[1].args[0]
        first_payload = json.loads(first_req.data.decode())
        retry_payload = json.loads(retry_req.data.decode())

        self.assertEqual(len(first_payload["messages"]), 4)
        self.assertEqual(len(retry_payload["messages"]), 2)
        self.assertEqual(retry_payload["max_tokens"], 256)


# ---------------------------------------------------------------------------
# OpenRouterTranslationEngine unit tests
# ---------------------------------------------------------------------------

class TestOpenRouterTranslationEngine(unittest.TestCase):

    def _engine(self) -> OpenRouterTranslationEngine:
        e = OpenRouterTranslationEngine.__new__(OpenRouterTranslationEngine)
        e._api_key = "fake-openrouter-key"
        e._model = "qwen/qwen3-30b-a3b-instruct-2507"
        e._timeout = 8
        e._max_tokens = 200
        e._strip_think = True
        return e

    def _response(self, content):
        import json
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = json.dumps({
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
        }).encode()
        return mock_resp

    def test_request_uses_openrouter_endpoint_headers_and_selected_model(self):
        import json

        e = self._engine()
        long_prompt = "full quality prompt " * 500

        with patch("urllib.request.urlopen", return_value=self._response("translated")) as urlopen:
            result = e.translate("source", long_prompt, False)

        self.assertEqual(result, "translated")
        req = urlopen.call_args.args[0]
        headers = {k.lower(): v for k, v in req.header_items()}
        payload = json.loads(req.data.decode())

        self.assertEqual(req.full_url, "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(headers["accept"], "application/json")
        self.assertEqual(headers["authorization"], "Bearer fake-openrouter-key")
        self.assertEqual(headers["user-agent"], "live_translate/1.0")
        self.assertEqual(headers["http-referer"], "http://localhost/live_translate")
        self.assertEqual(headers["x-title"], "live_translate")
        self.assertEqual(payload["model"], "qwen/qwen3-30b-a3b-instruct-2507")
        self.assertEqual(payload["reasoning"], {"exclude": True})
        self.assertEqual(payload["max_tokens"], 200)
        system_message = payload["messages"][0]["content"]
        self.assertIn("Korean to Traditional Chinese live subtitle translator", system_message)
        self.assertIn("Active streamer profile:", system_message)
        self.assertNotIn("full quality prompt", system_message)

    def test_limits_history_for_live_fallback(self):
        import json
        from config import cfg

        e = self._engine()
        history = [
            (f"source-{i}-" + "x" * 240, f"target-{i}-" + "y" * 300)
            for i in range(4)
        ]
        original_window = cfg.translation.openrouter_context_window

        try:
            object.__setattr__(cfg.translation, "openrouter_context_window", 2)
            with patch("urllib.request.urlopen", return_value=self._response("ok")) as urlopen:
                result = e.translate("hello", "system", False, history)
        finally:
            object.__setattr__(cfg.translation, "openrouter_context_window", original_window)

        self.assertEqual(result, "ok")
        req = urlopen.call_args.args[0]
        payload = json.loads(req.data.decode())
        messages = payload["messages"]

        self.assertEqual(len(messages), 6)
        self.assertNotIn("source-1-", str(messages))
        self.assertIn("source-2-", messages[1]["content"])
        self.assertIn("source-3-", messages[3]["content"])
        self.assertLessEqual(len(messages[1]["content"]), len("input: ") + 163)
        self.assertLessEqual(len(messages[2]["content"]), 223)

    def test_returns_none_for_reasoning_only_or_empty_content(self):
        e = self._engine()

        with patch("urllib.request.urlopen", return_value=self._response(None)):
            self.assertIsNone(e.translate("hello", "system", False))

        with patch("urllib.request.urlopen", return_value=self._response("<think>notes only")):
            self.assertIsNone(e.translate("hello", "system", False))


class TestOpenRouterFallbackChain(unittest.TestCase):
    def test_nvidia_backend_uses_openrouter_before_groq_fallback(self):
        from config import cfg

        class FakeEngine:
            def __init__(self, name: str):
                self._name = name

            @property
            def engine_name(self) -> str:
                return self._name

            @property
            def model_name(self) -> str:
                return f"{self._name}-model"

            @property
            def available(self) -> bool:
                return True

            def translate(self, text, system_prompt, incomplete, history=None):
                return "ok"

        class FakeNvidiaEngine(FakeEngine):
            def __init__(self):
                super().__init__("nvidia")

        class FakeOpenRouterEngine(FakeEngine):
            def __init__(self):
                super().__init__("openrouter")

        class FakeGroqEngine(FakeEngine):
            def __init__(self):
                super().__init__("groq")

        original_mode = cfg.translation.translation_mode
        original_chain = cfg.translation.engine_chain
        original_live_engine = cfg.live_engine
        try:
            object.__setattr__(cfg.translation, "translation_mode", "live")
            object.__setattr__(cfg.translation, "engine_chain", ("openrouter", "groq"))
            object.__setattr__(cfg, "live_engine", "nvidia")
            with patch.object(translation_engines_module, "NvidiaEngine", FakeNvidiaEngine), \
                 patch.object(translation_engines_module, "OpenRouterTranslationEngine", FakeOpenRouterEngine), \
                 patch.object(translation_engines_module, "GroqTranslationEngine", FakeGroqEngine):
                engines = _build_engine_chain()
        finally:
            object.__setattr__(cfg.translation, "translation_mode", original_mode)
            object.__setattr__(cfg.translation, "engine_chain", original_chain)
            object.__setattr__(cfg, "live_engine", original_live_engine)

        self.assertEqual([engine.engine_name for engine in engines], ["nvidia", "openrouter", "groq"])


# ---------------------------------------------------------------------------
# NvidiaEngine unit tests
# ---------------------------------------------------------------------------

class TestNvidiaEngine(unittest.TestCase):

    def _engine(self) -> NvidiaEngine:
        e = NvidiaEngine.__new__(NvidiaEngine)
        e._api_key = "fake-key"
        e._model = "qwen/qwen3-next-80b-a3b-instruct"
        e._timeout = 10
        e._is_qwen3 = True
        e._strip_think = True
        return e

    def _response(self, content: str):
        import json
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = json.dumps({
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }).encode()
        return mock_resp

    def test_default_live_timeout_fails_fast(self):
        from config import cfg

        self.assertEqual(cfg.nvidia.live_timeout, 5)

    def test_live_mode_uses_live_timeout(self):
        from config import cfg

        orig_mode = cfg.translation.translation_mode
        orig_key = cfg.keys.nvidia
        orig_timeout = cfg.nvidia.timeout
        orig_live_timeout = cfg.nvidia.live_timeout
        object.__setattr__(cfg.translation, "translation_mode", "live")
        object.__setattr__(cfg.keys, "nvidia", "fake-key")
        object.__setattr__(cfg.nvidia, "timeout", 10)
        object.__setattr__(cfg.nvidia, "live_timeout", 7)
        try:
            engine = NvidiaEngine()
        finally:
            object.__setattr__(cfg.translation, "translation_mode", orig_mode)
            object.__setattr__(cfg.keys, "nvidia", orig_key)
            object.__setattr__(cfg.nvidia, "timeout", orig_timeout)
            object.__setattr__(cfg.nvidia, "live_timeout", orig_live_timeout)

        self.assertEqual(engine._timeout, 7)

    def test_live_mode_default_falls_back_to_regular_timeout(self):
        from config import cfg

        orig_mode = cfg.translation.translation_mode
        orig_key = cfg.keys.nvidia
        orig_timeout = cfg.nvidia.timeout
        orig_live_timeout = cfg.nvidia.live_timeout
        object.__setattr__(cfg.translation, "translation_mode", "live")
        object.__setattr__(cfg.keys, "nvidia", "fake-key")
        object.__setattr__(cfg.nvidia, "timeout", 10)
        object.__setattr__(cfg.nvidia, "live_timeout", 0)
        try:
            engine = NvidiaEngine()
        finally:
            object.__setattr__(cfg.translation, "translation_mode", orig_mode)
            object.__setattr__(cfg.keys, "nvidia", orig_key)
            object.__setattr__(cfg.nvidia, "timeout", orig_timeout)
            object.__setattr__(cfg.nvidia, "live_timeout", orig_live_timeout)

        self.assertEqual(engine._timeout, 10)

    def test_live_mode_falls_back_to_regular_timeout_when_live_timeout_none(self):
        from config import cfg

        orig_mode = cfg.translation.translation_mode
        orig_key = cfg.keys.nvidia
        orig_timeout = cfg.nvidia.timeout
        orig_live_timeout = cfg.nvidia.live_timeout
        object.__setattr__(cfg.translation, "translation_mode", "live")
        object.__setattr__(cfg.keys, "nvidia", "fake-key")
        object.__setattr__(cfg.nvidia, "timeout", 10)
        object.__setattr__(cfg.nvidia, "live_timeout", None)
        try:
            engine = NvidiaEngine()
        finally:
            object.__setattr__(cfg.translation, "translation_mode", orig_mode)
            object.__setattr__(cfg.keys, "nvidia", orig_key)
            object.__setattr__(cfg.nvidia, "timeout", orig_timeout)
            object.__setattr__(cfg.nvidia, "live_timeout", orig_live_timeout)

        self.assertEqual(engine._timeout, 10)

    def test_clip_mode_uses_regular_timeout(self):
        from config import cfg

        orig_mode = cfg.translation.translation_mode
        orig_key = cfg.keys.nvidia
        orig_timeout = cfg.nvidia.timeout
        orig_live_timeout = cfg.nvidia.live_timeout
        object.__setattr__(cfg.translation, "translation_mode", "clip")
        object.__setattr__(cfg.keys, "nvidia", "fake-key")
        object.__setattr__(cfg.nvidia, "timeout", 10)
        object.__setattr__(cfg.nvidia, "live_timeout", 5)
        try:
            engine = NvidiaEngine()
        finally:
            object.__setattr__(cfg.translation, "translation_mode", orig_mode)
            object.__setattr__(cfg.keys, "nvidia", orig_key)
            object.__setattr__(cfg.nvidia, "timeout", orig_timeout)
            object.__setattr__(cfg.nvidia, "live_timeout", orig_live_timeout)

        self.assertEqual(engine._timeout, 10)

    def test_retries_once_after_empty_response(self):
        e = self._engine()
        side_effect = [self._response(""), self._response("你好")]

        with patch("urllib.request.urlopen", side_effect=side_effect) as urlopen, \
                patch("time.sleep") as sleep:
            result = e.translate("안녕하세요", "system", False)

        self.assertEqual(result, "你好")
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once()
        self.assertEqual(
            get_last_engine_diagnostics(),
            {"engine": "nvidia", "retry_count": 1, "retry_reason": "empty_response"},
        )
        api_diagnostics = get_last_engine_api_diagnostics()
        self.assertEqual(api_diagnostics["api_attempt_count"], 2)
        self.assertEqual(api_diagnostics["api_timeout_count"], 0)
        self.assertIsNotNone(api_diagnostics["api_total_wall_ms"])
        self.assertIsNotNone(api_diagnostics["api_final_attempt_ms"])
        self.assertGreaterEqual(api_diagnostics["retry_sleep_ms"], 0)
        self.assertEqual(api_diagnostics["timeout_config_ms"], 10000)
        self.assertIsNone(api_diagnostics["api_error_type"])
        self.assertIsNone(api_diagnostics["api_error_message_class"])

    def test_retries_once_after_timeout_error(self):
        import urllib.error

        e = self._engine()
        side_effect = [urllib.error.URLError("timed out"), self._response("你好")]

        with patch("urllib.request.urlopen", side_effect=side_effect) as urlopen, \
                patch("time.sleep") as sleep:
            result = e.translate("안녕하세요", "system", False)

        self.assertEqual(result, "你好")
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once()
        self.assertEqual(
            get_last_engine_diagnostics(),
            {"engine": "nvidia", "retry_count": 1, "retry_reason": "timeout"},
        )
        api_diagnostics = get_last_engine_api_diagnostics()
        self.assertEqual(api_diagnostics["api_attempt_count"], 2)
        self.assertEqual(api_diagnostics["api_timeout_count"], 1)
        self.assertIsNotNone(api_diagnostics["api_total_wall_ms"])
        self.assertIsNotNone(api_diagnostics["api_final_attempt_ms"])
        self.assertIsNotNone(api_diagnostics["api_first_attempt_ms"])
        self.assertIsNotNone(api_diagnostics["api_retry_attempt_ms"])
        self.assertGreaterEqual(api_diagnostics["retry_sleep_ms"], 0)
        self.assertEqual(api_diagnostics["timeout_config_ms"], 10000)
        self.assertEqual(api_diagnostics["api_attempt_timeout_ms"], 10000)
        self.assertEqual(api_diagnostics["api_attempt_index"], 2)
        self.assertEqual(api_diagnostics["api_inflight_count_at_start"], 0)
        self.assertIsNotNone(api_diagnostics["source_text_char_count"])
        self.assertEqual(api_diagnostics["prompt_char_count"], len("system"))
        self.assertGreater(api_diagnostics["request_body_char_count"], 0)
        self.assertEqual(api_diagnostics["message_count"], 2)
        self.assertEqual(api_diagnostics["context_item_count"], 0)
        self.assertIsNone(api_diagnostics["api_error_type"])
        self.assertIsNone(api_diagnostics["api_error_message_class"])

    def test_records_final_timeout_error_diagnostics(self):
        import urllib.error

        e = self._engine()
        side_effect = [urllib.error.URLError("timed out"), urllib.error.URLError("timed out")]

        with patch("urllib.request.urlopen", side_effect=side_effect) as urlopen, \
                patch("time.sleep") as sleep:
            result = e.translate("hello", "system", False)

        self.assertIsNone(result)
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once()
        self.assertEqual(
            get_last_engine_diagnostics(),
            {"engine": "nvidia", "retry_count": 1, "retry_reason": "timeout"},
        )
        api_diagnostics = get_last_engine_api_diagnostics()
        self.assertEqual(api_diagnostics["api_attempt_count"], 2)
        self.assertEqual(api_diagnostics["api_timeout_count"], 2)
        self.assertEqual(api_diagnostics["api_attempt_index"], 2)
        self.assertIsNotNone(api_diagnostics["api_first_attempt_ms"])
        self.assertIsNotNone(api_diagnostics["api_retry_attempt_ms"])
        self.assertEqual(api_diagnostics["api_error_type"], "timeout")
        self.assertEqual(api_diagnostics["api_error_message_class"], "read_timeout")
        self.assertEqual(api_diagnostics["timeout_config_ms"], 10000)

    def test_retries_once_after_non_timeout_network_error(self):
        import urllib.error

        e = self._engine()
        side_effect = [urllib.error.URLError("connection reset"), self._response("你好")]

        with patch("urllib.request.urlopen", side_effect=side_effect) as urlopen, \
                patch("time.sleep") as sleep:
            result = e.translate("안녕하세요", "system", False)

        self.assertEqual(result, "你好")
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once()
        self.assertEqual(
            get_last_engine_diagnostics(),
            {"engine": "nvidia", "retry_count": 1, "retry_reason": "network"},
        )

    def test_does_not_retry_rate_limit(self):
        import urllib.error

        e = self._engine()
        error = urllib.error.HTTPError(
            url="https://integrate.api.nvidia.com/v1/chat/completions",
            code=429,
            msg="rate limit",
            hdrs=None,
            fp=None,
        )

        with patch("urllib.request.urlopen", side_effect=error) as urlopen, \
                patch("time.sleep") as sleep:
            result = e.translate("안녕하세요", "system", False)

        self.assertIsNone(result)
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(
            get_last_engine_diagnostics(),
            {"engine": "nvidia", "retry_count": 0, "retry_reason": ""},
        )
        api_diagnostics = get_last_engine_api_diagnostics()
        self.assertEqual(api_diagnostics["api_attempt_count"], 1)
        self.assertEqual(api_diagnostics["api_timeout_count"], 0)
        self.assertEqual(api_diagnostics["api_error_type"], "api_error")
        self.assertEqual(api_diagnostics["api_error_message_class"], "http_4xx")

    def test_does_not_retry_http_server_error(self):
        import urllib.error

        e = self._engine()
        error = urllib.error.HTTPError(
            url="https://integrate.api.nvidia.com/v1/chat/completions",
            code=500,
            msg="server error",
            hdrs=None,
            fp=None,
        )

        with patch("urllib.request.urlopen", side_effect=error) as urlopen, \
                patch("time.sleep") as sleep:
            result = e.translate("안녕하세요", "system", False)

        self.assertIsNone(result)
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(
            get_last_engine_diagnostics(),
            {"engine": "nvidia", "retry_count": 0, "retry_reason": ""},
        )
        api_diagnostics = get_last_engine_api_diagnostics()
        self.assertEqual(api_diagnostics["api_attempt_count"], 1)
        self.assertEqual(api_diagnostics["api_timeout_count"], 0)
        self.assertEqual(api_diagnostics["api_error_type"], "api_error")
        self.assertEqual(api_diagnostics["api_error_message_class"], "http_5xx")


class TestRuntimeRetryAttribution(unittest.TestCase):
    def _emit_outcome_with_stale_nvidia_diagnostics(
        self,
        outcome: TranslationOutcome,
        api_diagnostics: dict | None = None,
    ) -> dict:
        sentence_q = queue.Queue()
        subtitle_q = queue.Queue()
        stop = threading.Event()

        class _FakeTranslator:
            def __init__(self, shared_state=None):
                pass

            def translate_event(self, text: str, incomplete: bool = False) -> TranslationOutcome:
                return outcome

        stale_diagnostics = {
            "engine": "nvidia",
            "retry_count": 1,
            "retry_reason": "timeout",
        }
        api_diagnostics = api_diagnostics or {
            "engine": "nvidia",
            "api_attempt_count": 0,
        }
        with patch.object(translator_module, "Translator", _FakeTranslator), \
                patch.object(translator_module, "get_last_engine_diagnostics", return_value=stale_diagnostics), \
                patch.object(translator_module, "get_last_engine_api_diagnostics", return_value=api_diagnostics), \
                patch.object(translator_module, "runtime_events") as events:
            thread = translator_module.start(sentence_q, subtitle_q, stop)
            sentence_q.put({"text": outcome.source_text, "incomplete": outcome.incomplete})
            if outcome.target_text:
                self.assertEqual(subtitle_q.get(timeout=3), outcome.target_text)
            deadline = time.monotonic() + 3
            while not events.emit.called and time.monotonic() < deadline:
                stop.wait(0.005)
            stop.set()
            thread.join(timeout=2)

        events.emit.assert_called_once()
        return events.emit.call_args.kwargs

    def test_translation_workers_share_translator_state(self):
        sentence_q = queue.Queue()
        subtitle_q = queue.Queue()
        stop = threading.Event()
        first_started = threading.Event()
        second_started = threading.Event()
        release_first = threading.Event()

        class _SharedFakeTranslator:
            instances: list["_SharedFakeTranslator"] = []
            calls: list[str] = []
            lock = threading.Lock()

            def __init__(self, shared_state=None):
                self.shared_state = shared_state
                with self.__class__.lock:
                    self.__class__.instances.append(self)

            def translate_event(self, text: str, incomplete: bool = False) -> TranslationOutcome:
                with self.__class__.lock:
                    self.__class__.calls.append(text)
                if text == "first":
                    first_started.set()
                    second_started.wait(timeout=3)
                    release_first.wait(timeout=3)
                else:
                    second_started.set()
                return TranslationOutcome(
                    source_text=text,
                    target_text=f"out-{text}",
                    status="success",
                    result_source="api",
                    cache_status="miss",
                    incomplete=incomplete,
                    engine="fake",
                    model="fake-model",
                )

        with patch.object(translator_module, "Translator", _SharedFakeTranslator), \
                patch.object(translator_module, "runtime_events") as events:
            thread = translator_module.start(sentence_q, subtitle_q, stop)
            try:
                sentence_q.put({"text": "first", "incomplete": False})
                self.assertTrue(first_started.wait(timeout=3))
                sentence_q.put({"text": "second", "incomplete": False})
                self.assertTrue(second_started.wait(timeout=3))
                release_first.set()

                deadline = time.monotonic() + 3
                while events.emit.call_count < 2 and time.monotonic() < deadline:
                    stop.wait(0.005)
            finally:
                stop.set()
                thread.join(timeout=2)

        self.assertGreaterEqual(len(_SharedFakeTranslator.instances), 2)
        shared_state_ids = {id(instance.shared_state) for instance in _SharedFakeTranslator.instances}
        self.assertEqual(len(shared_state_ids), 1)
        self.assertIsNotNone(_SharedFakeTranslator.instances[0].shared_state)
        self.assertCountEqual(_SharedFakeTranslator.calls, ["first", "second"])
        self.assertEqual(events.emit.call_count, 2)

    def test_stale_translation_skips_subtitle_display(self):
        sentence_q = queue.Queue()
        subtitle_q = queue.Queue()
        stop = threading.Event()
        orig_delay = getattr(translator_module.cfg.translation, "max_subtitle_output_delay_ms", 30000)

        class _SlowFakeTranslator:
            def __init__(self, shared_state=None):
                pass

            def translate_event(self, text: str, incomplete: bool = False) -> TranslationOutcome:
                time.sleep(0.05)
                return TranslationOutcome(
                    source_text=text,
                    target_text=f"out-{text}",
                    status="success",
                    result_source="api",
                    cache_status="miss",
                    incomplete=incomplete,
                    engine="fake",
                    model="fake-model",
                )

        object.__setattr__(translator_module.cfg.translation, "max_subtitle_output_delay_ms", 1)
        try:
            with patch.object(translator_module, "Translator", _SlowFakeTranslator), \
                    patch.object(translator_module, "runtime_events") as events:
                thread = translator_module.start(sentence_q, subtitle_q, stop)
                sentence_q.put({"text": "late", "incomplete": False})
                deadline = time.monotonic() + 3
                while not events.emit.called and time.monotonic() < deadline:
                    stop.wait(0.005)
                stop.set()
                thread.join(timeout=2)
        finally:
            object.__setattr__(
                translator_module.cfg.translation,
                "max_subtitle_output_delay_ms",
                orig_delay,
            )

        self.assertTrue(subtitle_q.empty())
        events.emit.assert_called_once()
        self.assertFalse(events.emit.call_args.kwargs["subtitle_emitted"])
        self.assertEqual(events.emit.call_args.kwargs["subtitle_suppressed_reason"], "stale_output_delay")
        self.assertGreater(events.emit.call_args.kwargs["output_delay_ms"], 1)

    def test_memory_hit_ignores_stale_nvidia_retry_diagnostics(self):
        event = self._emit_outcome_with_stale_nvidia_diagnostics(
            TranslationOutcome(
                source_text="안녕하세요",
                target_text="你好",
                status="success",
                result_source="memory_hit",
                cache_status="memory_hit",
                incomplete=False,
                engine="nvidia",
                model="nvidia-test",
            )
        )

        self.assertEqual(event["retry_count"], 0)
        self.assertEqual(event["retry_reason"], "")

    def test_slang_hit_ignores_stale_nvidia_retry_diagnostics(self):
        event = self._emit_outcome_with_stale_nvidia_diagnostics(
            TranslationOutcome(
                source_text="ㅋㅋㅋ",
                target_text="哈哈哈",
                status="success",
                result_source="slang",
                cache_status="skipped",
                incomplete=False,
                engine="nvidia",
                model="nvidia-test",
            )
        )

        self.assertEqual(event["retry_count"], 0)
        self.assertEqual(event["retry_reason"], "")

    def test_api_result_keeps_nvidia_retry_diagnostics(self):
        event = self._emit_outcome_with_stale_nvidia_diagnostics(
            TranslationOutcome(
                source_text="안녕하세요",
                target_text="你好",
                status="success",
                result_source="api",
                cache_status="miss",
                incomplete=False,
                engine="nvidia",
                model="nvidia-test",
            )
        )

        self.assertEqual(event["retry_count"], 1)
        self.assertEqual(event["retry_reason"], "timeout")

    def test_api_result_emits_nvidia_api_diagnostics(self):
        event = self._emit_outcome_with_stale_nvidia_diagnostics(
            TranslationOutcome(
                source_text="hello",
                target_text="translated",
                status="success",
                result_source="api",
                cache_status="miss",
                incomplete=False,
                engine="nvidia",
                model="nvidia-test",
            ),
            api_diagnostics={
                "engine": "nvidia",
                "api_attempt_count": 2,
                "api_timeout_count": 1,
                "api_total_wall_ms": 20500.0,
                "api_final_attempt_ms": 9950.0,
                "api_first_attempt_ms": 10000.0,
                "api_retry_attempt_ms": 9950.0,
                "retry_sleep_ms": 500.0,
                "timeout_config_ms": 10000.0,
                "api_attempt_timeout_ms": 10000.0,
                "api_attempt_index": 2,
                "api_inflight_count_at_start": 1,
                "source_text_char_count": 5,
                "prompt_char_count": 42,
                "request_body_char_count": 1234,
                "message_count": 4,
                "context_item_count": 1,
                "api_error_type": None,
                "api_error_message_class": None,
            },
        )

        self.assertEqual(event["api_attempt_count"], 2)
        self.assertEqual(event["api_timeout_count"], 1)
        self.assertEqual(event["api_total_wall_ms"], 20500.0)
        self.assertEqual(event["api_final_attempt_ms"], 9950.0)
        self.assertEqual(event["api_first_attempt_ms"], 10000.0)
        self.assertEqual(event["api_retry_attempt_ms"], 9950.0)
        self.assertEqual(event["retry_sleep_ms"], 500.0)
        self.assertEqual(event["timeout_config_ms"], 10000.0)
        self.assertEqual(event["api_attempt_timeout_ms"], 10000.0)
        self.assertEqual(event["api_attempt_index"], 2)
        self.assertEqual(event["api_inflight_count_at_start"], 1)
        self.assertEqual(event["source_text_char_count"], 5)
        self.assertEqual(event["prompt_char_count"], 42)
        self.assertEqual(event["request_body_char_count"], 1234)
        self.assertEqual(event["message_count"], 4)
        self.assertEqual(event["context_item_count"], 1)
        self.assertIsNone(event["api_error_type"])
        self.assertIsNone(event["api_error_message_class"])


# ---------------------------------------------------------------------------
# History file
# ---------------------------------------------------------------------------

class TestWriteHistory(unittest.TestCase):

    def test_creates_file_with_ko_and_zh(self):
        import modules.translator as tmod
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(tmod, "_LOG_DIR", __import__("pathlib").Path(tmpdir)):
                _write_history("안녕하세요", "你好")
            files = list(__import__("pathlib").Path(tmpdir).glob("translations_*.txt"))
            self.assertEqual(len(files), 1)
            content = files[0].read_text(encoding="utf-8")
            self.assertIn("안녕하세요", content)
            self.assertIn("你好", content)

    def test_appends_multiple_entries(self):
        import modules.translator as tmod
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(tmod, "_LOG_DIR", __import__("pathlib").Path(tmpdir)):
                _write_history("첫 번째", "第一")
                _write_history("두 번째", "第二")
            files = list(__import__("pathlib").Path(tmpdir).glob("translations_*.txt"))
            content = files[0].read_text(encoding="utf-8")
            self.assertIn("첫 번째", content)
            self.assertIn("두 번째", content)


# ---------------------------------------------------------------------------
# Pause behaviour
# ---------------------------------------------------------------------------

class TestTranslatorThreadPause(unittest.TestCase):

    def test_pause_prevents_translation(self):
        from modules.translator import start as translator_start
        sentence_q: queue.Queue = queue.Queue()
        subtitle_q: queue.Queue = queue.Queue()
        stop = threading.Event()
        pause = threading.Event()
        pause.set()   # start paused

        with patch("anthropic.Anthropic") as mock_cls:
            mock_cls.return_value.messages.create.return_value = _claude_resp("你好")
            translator_start(sentence_q, subtitle_q, stop, pause_event=pause)
            sentence_q.put({"text": "안녕하세요", "incomplete": False})
            time.sleep(0.5)
            stop.set()

        self.assertTrue(subtitle_q.empty(), "No output expected while paused")


# ---------------------------------------------------------------------------
# Fallback probe logic
# ---------------------------------------------------------------------------

class TestFallbackProbe(unittest.TestCase):

    def test_user_translation_path_does_not_probe_primary_after_recovery(self):
        from modules.translator import _FALLBACK_PROBE_EVERY
        t = _make_translator()
        t._active_idx = 1                            # currently on fallback
        t._probe_counter = _FALLBACK_PROBE_EVERY - 1 # one away from probe
        t._engines[0].translate.return_value = "你好"  # primary recovered
        t._engines[1].translate.return_value = "fallback"

        result = t.translate("안녕하세요")
        self.assertEqual(result, "fallback")
        self.assertEqual(t._active_idx, 1, "User translation path should stay on fallback until background probe")
        t._engines[0].translate.assert_not_called()

    def test_user_translation_path_stays_on_fallback_without_probe(self):
        from modules.translator import _FALLBACK_PROBE_EVERY
        t = _make_translator()
        t._active_idx = 1
        t._probe_counter = _FALLBACK_PROBE_EVERY - 1
        t._engines[0].translate.return_value = None   # primary still down
        t._engines[1].translate.return_value = "fallback result"

        result = t.translate("안녕하세요")
        self.assertEqual(result, "fallback result")
        self.assertEqual(t._active_idx, 1, "Should stay on fallback if probe fails")
        t._engines[0].translate.assert_not_called()

    def test_background_probe_thread_restores_primary(self):
        shared = translator_module._new_translator_shared_state()
        shared.fallback.active_idx = 1
        stop = threading.Event()
        engines = [_mock_engine("primary", "你好"), _mock_engine("fallback", "fallback")]

        with patch.object(translator_module, "_build_engine_chain", return_value=engines):
            thread = translator_module._start_fallback_probe_thread(
                shared,
                stop,
                interval_seconds=0.01,
            )
            deadline = time.monotonic() + 1.0
            while shared.fallback.active_idx != 0 and time.monotonic() < deadline:
                stop.wait(0.005)
            stop.set()
            thread.join(timeout=1)

        self.assertEqual(shared.fallback.active_idx, 0)
        engines[0].translate.assert_called()

    def test_single_failure_uses_fallback_without_switching(self):
        t = _make_translator()
        t._engines[0].translate.return_value = None
        t._engines[1].translate.return_value = "哈囉"

        result = t.translate("안녕하세요")
        self.assertEqual(result, "哈囉")
        self.assertEqual(t._active_idx, 0, "single failure should not hard-switch primary")
        self.assertEqual(t._consecutive_primary_failures, 1)

    def test_hard_switch_after_threshold_failures(self):
        from modules.translator import _FALLBACK_THRESHOLD
        t = _make_translator()
        t._engines[0].translate.return_value = None
        t._engines[1].translate.return_value = "哈囉"

        # Use distinct inputs — duplicate-suppression policy would block identical consecutive calls
        for i in range(_FALLBACK_THRESHOLD):
            t.translate(f"안녕하세요 {i}")

        self.assertEqual(t._active_idx, 1, "should hard-switch after threshold consecutive failures")
        self.assertEqual(t._consecutive_primary_failures, 0, "counter resets after hard switch")

    def test_success_resets_failure_counter(self):
        t = _make_translator()
        t._consecutive_primary_failures = 2
        t._engines[0].translate.return_value = "你好"

        result = t.translate("안녕하세요")
        self.assertEqual(result, "你好")
        self.assertEqual(t._consecutive_primary_failures, 0, "success should reset failure counter")
        self.assertEqual(t._active_idx, 0, "primary should remain active")

    def test_concurrent_state_merge_does_not_regress_failures(self):
        shared = translator_module.FallbackState(
            active_idx=0,
            probe_counter=0,
            consecutive_primary_failures=2,
        )
        before = translator_module.FallbackState(
            active_idx=0,
            probe_counter=0,
            consecutive_primary_failures=0,
        )
        after = translator_module.FallbackState(
            active_idx=0,
            probe_counter=0,
            consecutive_primary_failures=0,
        )

        translator_module._merge_fallback_state(shared, before, after)

        self.assertEqual(shared.active_idx, 0)
        self.assertEqual(shared.consecutive_primary_failures, 2)

    def test_concurrent_state_merge_preserves_hard_switch(self):
        shared = translator_module.FallbackState(
            active_idx=1,
            probe_counter=0,
            consecutive_primary_failures=0,
        )
        before = translator_module.FallbackState(
            active_idx=0,
            probe_counter=0,
            consecutive_primary_failures=0,
        )
        after = translator_module.FallbackState(
            active_idx=0,
            probe_counter=0,
            consecutive_primary_failures=0,
        )

        translator_module._merge_fallback_state(shared, before, after)

        self.assertEqual(shared.active_idx, 1)
        self.assertEqual(shared.consecutive_primary_failures, 0)


# ---------------------------------------------------------------------------
# Cache, slang, and short-input optimisations
# ---------------------------------------------------------------------------

class TestTranslateOptimizations(unittest.TestCase):

    def test_short_input_returns_none(self):
        t = _make_translator()
        self.assertIsNone(t.translate("a"))
        self.assertIsNone(t.translate(" "))
        self.assertIsNone(t.translate(""))

    def test_short_input_does_not_call_api(self):
        t = _make_translator()
        t.translate("a")
        for engine in t._engines:
            engine.translate.assert_not_called()

    def test_prepare_input_strips_and_suppresses_duplicate(self):
        t = _make_translator()
        self.assertEqual(t._prepare_input("  안녕하세요  "), "안녕하세요")
        self.assertIsNone(t._prepare_input("안녕하세요"))

    def test_slang_returns_direct_without_api(self):
        from config import cfg
        t = _make_translator()
        ko, zh = next(iter(cfg.translation.slang.items()))
        result = t.translate(ko)
        self.assertEqual(result, zh)
        for engine in t._engines:
            engine.translate.assert_not_called()

    def test_translate_event_reports_api_metadata(self):
        t = _make_translator()
        t._engines[0].translate.return_value = "你好"

        outcome = t.translate_event("안녕하세요")

        self.assertEqual(outcome.status, "success")
        self.assertEqual(outcome.result_source, "api")
        # live mode default: DB cache layer disabled -> lookup reports "skipped"
        self.assertEqual(outcome.cache_status, "skipped")
        self.assertEqual(outcome.engine, "gemini")
        self.assertEqual(outcome.model, "gemini-test-model")
        self.assertEqual(outcome.target_text, "你好")

    def test_translate_event_reports_filtered_reason(self):
        t = _make_translator()

        outcome = t.translate_event("a")

        self.assertEqual(outcome.status, "filtered")
        self.assertEqual(outcome.result_source, "policy")
        self.assertEqual(outcome.filter_reason, "too_short")
        for engine in t._engines:
            engine.translate.assert_not_called()

    def test_meta_garbage_engine_output_is_filtered(self):
        t = _make_translator()
        t._engines[0].translate.return_value = "（無法理解的STT亂碼，無明確語義）"
        t._record_success = MagicMock()

        outcome = t.translate_event("오늘 방송 진짜 재미있었어요")

        self.assertEqual(outcome.status, "filtered")
        self.assertEqual(outcome.result_source, "post_policy")
        self.assertEqual(outcome.filter_reason, "meta_garbage_output")
        self.assertIsNone(outcome.target_text)
        t._record_success.assert_not_called()

    def test_cached_meta_garbage_output_is_filtered(self):
        t = _make_translator()
        engine = t._active_engine()
        prompt_ver = t._get_prompt_version_hash()
        source = "오늘 방송 진짜 재미있었어요"
        t._memory.cache_store(
            source,
            False,
            "（無法理解的STT亂碼，無明確語義）",
            prompt_ver,
            engine,
        )

        outcome = t.translate_event(source)

        self.assertEqual(outcome.status, "filtered")
        self.assertEqual(outcome.result_source, "post_policy")
        self.assertEqual(outcome.cache_status, "memory_hit")
        self.assertEqual(outcome.filter_reason, "meta_garbage_output")
        self.assertIsNone(t._memory.cache_lookup(source, False, prompt_ver, engine))
        self.assertEqual(list(t._memory.recent), [])
        for engine in t._engines:
            engine.translate.assert_not_called()

    def test_source_aware_corrections_fix_runtime_term_misfires(self):
        source = (
            "마가뜨는 게 정확히 말하는 거. 붕 뜨는 시간. 개복치같은 스타일. "
            "하데스. 끼윤이랑 예난이. 철구형. 갓 신내림 받은 무당이 더 신발 좋은 거 아시죠? 거의 만신이십니다."
        )
        result = (
            "瑪加特才是精確說的。飄起來的時間。鯛魚燒風格。哈迪斯。"
            "끼윤和藝蘭。鐵球哥。剛受神降的巫女不是更懂鞋嗎？幾乎都滿了，滿了。"
        )

        corrected = _apply_source_aware_corrections(source, result)

        self.assertEqual(
            corrected,
            "冷場才是精確說的。空掉的時間。玻璃心風格。HADES。"
            "Kkiyun和Yenan。Chulgu哥。剛受神降的巫女不是神力更強嗎？幾乎是大神巫。",
        )

    def test_source_aware_corrections_fix_current_hades_hot_terms(self):
        cases = (
            (
                "메이플은 좀 아기자기하네?",
                "《仙境傳說》確實有點萌萌的。",
                "《楓之谷》確實有點萌萌的。",
            ),
            (
                "메이플은 좀 아기자기하네?",
                "MapleStory確實有點萌萌的。",
                "楓之谷確實有點萌萌的。",
            ),
            (
                "프린세스 메이커 갓겜임.",
                "《公主製造》真是神作。",
                "《美少女夢工場》真是神作。",
            ),
            (
                "포스터가 피맛이 너무 느껴져서",
                "海報實在太有酒味了。",
                "海報實在太有血味了。",
            ),
            (
                "창나면 일단 사과부터 하면",
                "如果開戰，先道歉。",
                "如果出事，先道歉。",
            ),
            (
                "다 죽여버릴 것 같은 그런 느낌",
                "這有種讓人想死的感覺。",
                "這有種殺氣很重的感覺。",
            ),
        )

        for source, target, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(_apply_source_aware_corrections(source, target), expected)

    def test_stellive_hina_current_stream_terms_are_corrected(self):
        cases = (
            ("해둥이 금지", "海洞禁止", "해둥이禁止"),
            ("해둥아 너를 처음 본 순간부터", "海洞啊，從第一次見到你", "해둥아，從第一次見到你"),
            ("유니 선배 생일이구나", "優妮前輩生日啊", "Yuni前輩生日啊"),
            ("그냥 유니가 1기생이다", "Yuni是個日記生", "Yuni是個1期生"),
            ("히나유키 히나가 시켰다고 할게", "希拉尤基·Hina叫的", "Shirayuki Hina叫的"),
            ("메이플 하고 싶다", "想玩 Maple", "想玩 楓之谷"),
            ("투니버스 메들리 보고 들어왔어요", "看了Touriverus Madeline才進來的", "看了투니버스 메들리才進來的"),
        )

        with _active_translation_profile("stellive_hina"):
            for source, target, expected in cases:
                with self.subTest(source=source, target=target):
                    self.assertEqual(_apply_source_aware_corrections(source, target), expected)

    def test_stellive_hina_current_stream_terms_are_profile_gated(self):
        cases = (
            ("해둥이 금지", "海洞禁止"),
            ("유니 선배 생일이구나", "優妮前輩生日啊"),
            ("히나유키 히나가 시켰다고 할게", "希拉尤基·Hina叫的"),
            ("투니버스 메들리 보고 들어왔어요", "看了Touriverus Madeline才進來的"),
        )

        for profile_id in ("", "hades_chxxnnx", "mwmeu"):
            with self.subTest(profile_id=profile_id):
                with _active_translation_profile(profile_id):
                    for source, target in cases:
                        self.assertEqual(_apply_source_aware_corrections(source, target), target)

    def test_source_aware_corrections_do_not_restore_stock_streamer_phrase(self):
        source = "내일 서버 설명회 때 한번 오시면 되겠습니다. 구독과 좋아요는 저에게 아주 큰 힘이 됩니다."
        target = "明天伺服器說明會時來一趟就好。"

        self.assertEqual(
            _apply_source_aware_corrections(source, target),
            target,
        )

    def test_mwmeu_name_rendering_fixes_runtime_variants(self):
        cases = (
            ("이비가 찾은 거예요", "伊比姐姐找到了", "이비姐姐找到了"),
            ("이비 언니랑 같이 해요", "李比姐姐一起玩", "이비姐姐一起玩"),
            ("수아가 답변했어요", "數亞姐姐回答了", "수아姐姐回答了"),
            ("리츠랑 초은이가 앉았어요", "利茨和初雲坐下了", "리츠和초은坐下了"),
            ("리츠와 아이들이요", "Rits與小孩們", "리츠與小孩們"),
            ("리츠가 많이 힘들었어", "リツ好像很累", "리츠好像很累"),
            ("리츠 언니가 했어요", "Ritz姐姐做了", "리츠姐姐做了"),
            ("지한 언니가 말했어요", "志安姐姐說了", "지한姐姐說了"),
            ("지한이가 왔어요", "Z-Han來了", "지한來了"),
            ("웬즈들이 가까이서 봤어요", "wenz們近距離看到了", "WENs們近距離看到了"),
        )

        with _active_translation_profile("mwmeu"):
            for source, target, expected in cases:
                with self.subTest(source=source, target=target):
                    self.assertEqual(_apply_source_aware_corrections(source, target), expected)

    def test_mwmeu_chiikawa_rendering_fixes_runtime_variants(self):
        source = "치이카와랑 하치와레랑 모몽가가 나왔어요"
        target = "千川和哈奇瓦還有毛毛蟲都出來了"

        with _active_translation_profile("mwmeu"):
            self.assertEqual(
                _apply_source_aware_corrections(source, target),
                "Chiikawa和Hachiware還有Momonga都出來了",
            )

    def test_mwmeu_japanese_phrase_near_miss_is_corrected(self):
        source = "다이저고 데스. 아리가또 고자이마스. 이러는 거야."
        target = "一進去就直接說：「Daisuki desu！Arigatou gozaimasu！」"

        with _active_translation_profile("mwmeu"):
            self.assertEqual(
                _apply_source_aware_corrections(source, target),
                "一進去就直接說：「大丈夫です！Arigatou gozaimasu！」",
            )

    def test_mwmeu_current_stream_hot_terms_are_corrected(self):
        cases = (
            ("오버쿡드 2 할게요", "Oma-kooks 立刻投！投！", "《胡鬧廚房2》"),
            ("어금니 같아요", "像牙齦", "像臼齒"),
            ("짬밥순이 아니었어?", "不是按餃子順序來的嗎？", "不是按資歷順來的嗎？"),
            ("땡글즈 플러스 이제", "Tanggulz Plus怡潔", "땡글즈 Plus 이제"),
            ("겟머츠 아니고 땡글즈예요", "不是GetMuts，是Tanggulz", "不是겟머츠，是땡글즈"),
            ("띠빵뽕 버스기사", "叮糖餅司機", "띠빵뽕司機"),
            ("신호등즈가 맞나?", "信號燈們對嗎？", "信號燈즈對嗎？"),
            ("리츠 선배는 했어요", "リツ生趴伊做到了", "리츠前輩做到了"),
        )

        with _active_translation_profile("mwmeu"):
            for source, target, expected in cases:
                with self.subTest(source=source, target=target):
                    self.assertEqual(_apply_source_aware_corrections(source, target), expected)

    def test_mwmeu_name_rendering_is_profile_gated(self):
        source = "리츠랑 초은이가 앉았어요"
        target = "利茨和初雲坐下了"

        with _active_translation_profile("hades_chxxnnx"):
            self.assertEqual(_apply_source_aware_corrections(source, target), target)
        with _active_translation_profile("mwmeu", use_profile=False):
            self.assertEqual(_apply_source_aware_corrections(source, target), target)

    def test_streamer_name_rendering_boundary_positive_cases(self):
        cases = (
            ("챈나가 멤버 섭외", "快叫醒-chan", "快叫醒Chxxnnx"),
            ("챈나님 오늘 와요", "謝謝-chan", "謝謝Chxxnnx"),
            ("성태는 지금 와요", "Sungtae哥來了", "KimSungtae來了"),
            ("성태형 불러요", "Sungtae老師來了", "KimSungtae來了"),
            ("봉준이 왔어요", "Bongjun來了", "Kim Bongjun來了"),
            ("봉준님 불러요", "奉主來了", "Kim Bongjun來了"),
            ("김봉준이 말했어요", "奉俊說了", "Kim Bongjun說了"),
            ("키마는 대기 중", "Kima待機中", "Kyma待機中"),
            ("큐마는 대기 중", "큐마待機中", "Kyma待機中"),
            ("솜주먹 바보님", "桑拳頭笨蛋", "Sompunch笨蛋"),
            ("솜주먹 언니 와요", "拳頭姐姐來了", "Sompunch姐姐來了"),
            ("김띵귤 기강 잡아라", "金叮菊管管紀律", "Singgyul管管紀律"),
            ("띵귤이 왔어요", "TINGGYUL來了", "Singgyul來了"),
            ("띵띵이도 친구 많아", "Singgyul朋友很多", "띵띵이朋友很多"),
            ("김챗나 방", "金chat的房間", "Chxxnnx的房間"),
            ("챈나가 왔어요", "Chaenna來了", "Chxxnnx來了"),
            ("챈나가 왔어요", "CHXXNNX來了", "Chxxnnx來了"),
            ("고세구가 왔어요", "高世久來了", "Gosegu來了"),
        )

        with _active_translation_profile("hades_chxxnnx"):
            for source, target, expected in cases:
                with self.subTest(source=source, target=target):
                    self.assertEqual(_apply_source_aware_corrections(source, target), expected)

    def test_streamer_name_rendering_hangul_self_forms(self):
        cases = (
            ("챈나가 멤버 섭외", "챈나好可愛", "Chxxnnx好可愛"),
            ("봉준이 왔어요", "봉준來了", "Kim Bongjun來了"),
            ("김봉준이 말했어요", "김봉준說了", "Kim Bongjun說了"),
            ("성태는 지금 와요", "성태來了", "KimSungtae來了"),
            ("키마는 대기 중", "키마待機中", "Kyma待機中"),
            ("솜펀치 언니 와요", "솜펀치姐姐來了", "Sompunch姐姐來了"),
            ("띵귤이 왔어요", "띵귤來了", "Singgyul來了"),
        )

        with _active_translation_profile("hades_chxxnnx"):
            for source, target, expected in cases:
                with self.subTest(source=source, target=target):
                    self.assertEqual(_apply_source_aware_corrections(source, target), expected)

    def test_streamer_name_rendering_boundary_negative_cases(self):
        cases = (
            ("김챈나가 왔어요", "快叫醒-chan"),
            ("성태권도 이야기", "Sungtae哥"),
            ("가성태님이 왔어요", "Sungtae老師"),
            ("박봉준이 왔어요", "Bongjun"),
            ("챈나님abc", "-chan"),
            ("오늘 정상적인 한국어 문장입니다", "Bongjun Sungtae 高世久"),
        )

        with _active_translation_profile("hades_chxxnnx"):
            for source, target in cases:
                with self.subTest(source=source, target=target):
                    self.assertEqual(_apply_source_aware_corrections(source, target), target)

    def test_streamer_name_rendering_is_source_gated(self):
        with _active_translation_profile("hades_chxxnnx"):
            self.assertEqual(_apply_source_aware_corrections("오늘 방송 재미있다", "-chan"), "-chan")
            self.assertEqual(_apply_source_aware_corrections("오늘 방송 재미있다", "Bongjun"), "Bongjun")
            self.assertEqual(_apply_source_aware_corrections("오늘 방송 재미있다", "高世久"), "高世久")
            self.assertEqual(_apply_source_aware_corrections("오늘 방송 재미있다", "챈나好可愛"), "챈나好可愛")
            self.assertEqual(_apply_source_aware_corrections("오늘 방송 재미있다", "봉준來了"), "봉준來了")
            self.assertEqual(_apply_source_aware_corrections("오늘 방송 재미있다", "김봉준說了"), "김봉준說了")
            self.assertEqual(_apply_source_aware_corrections("오늘 방송 재미있다", "성태來了"), "성태來了")
            self.assertEqual(_apply_source_aware_corrections("오늘 방송 재미있다", "키마待機中"), "키마待機中")

    def test_streamer_name_rendering_is_profile_gated(self):
        hades_self_form_cases = (
            ("챈나가 왔어요", "챈나"),
            ("봉준이 왔어요", "봉준"),
            ("김봉준이 말했어요", "김봉준"),
            ("성태는 왔어요", "성태"),
            ("키마는 왔어요", "키마"),
        )

        for profile_id in ("", "stellive_hina", "isegye_lilpa"):
            with self.subTest(profile_id=profile_id):
                with _active_translation_profile(profile_id):
                    self.assertEqual(
                        _apply_source_aware_corrections("챈나가 왔어요", "-chan"),
                        "-chan",
                    )
                    self.assertEqual(
                        _apply_source_aware_corrections("봉준이 왔어요", "Bongjun"),
                        "Bongjun",
                    )
                    for source, target in hades_self_form_cases:
                        self.assertEqual(_apply_source_aware_corrections(source, target), target)

        with _active_translation_profile("hades_chxxnnx", use_profile=False):
            self.assertEqual(_apply_source_aware_corrections("성태는 왔어요", "Sungtae"), "Sungtae")
            for source, target in hades_self_form_cases:
                self.assertEqual(_apply_source_aware_corrections(source, target), target)

        with _active_translation_profile("stellive_hina", use_profile=False):
            self.assertEqual(_apply_source_aware_corrections("고세구가 왔어요", "高世久"), "Gosegu")

    def test_bare_hina_existing_entry_is_unchanged(self):
        with _active_translation_profile("hades_chxxnnx"):
            self.assertEqual(_apply_source_aware_corrections("히나", "希娜"), "Hina")

    def test_streamer_name_rendering_is_idempotent(self):
        cases = (
            ("챈나가 왔어요", "-chan", "Chxxnnx"),
            ("봉준이 왔어요", "Bongjun", "Kim Bongjun"),
            ("성태는 왔어요", "Sungtae哥", "KimSungtae"),
            ("키마는 왔어요", "Kima", "Kyma"),
            ("고세구가 왔어요", "高世久", "Gosegu"),
        )

        with _active_translation_profile("hades_chxxnnx"):
            for source, target, expected in cases:
                with self.subTest(source=source, target=target):
                    once = _apply_source_aware_corrections(source, target)
                    twice = _apply_source_aware_corrections(source, once)
                    self.assertEqual(once, expected)
                    self.assertEqual(twice, once)

            self.assertEqual(_apply_source_aware_corrections("봉준이 왔어요", "Kim Bongjun"), "Kim Bongjun")
            self.assertEqual(_apply_source_aware_corrections("성태는 왔어요", "KimSungtae"), "KimSungtae")
            self.assertEqual(_apply_source_aware_corrections("챈나가 왔어요", "Chxxnnx好可愛"), "Chxxnnx好可愛")
            self.assertEqual(_apply_source_aware_corrections("키마는 왔어요", "Kyma待機中"), "Kyma待機中")

    def test_streamer_name_rendering_self_forms_are_idempotent(self):
        cases = (
            ("챈나가 왔어요", "챈나好可愛", "Chxxnnx好可愛"),
            ("봉준이 왔어요", "봉준來了", "Kim Bongjun來了"),
            ("김봉준이 말했어요", "김봉준說了", "Kim Bongjun說了"),
            ("성태는 왔어요", "성태來了", "KimSungtae來了"),
            ("키마는 왔어요", "키마待機中", "Kyma待機中"),
        )

        with _active_translation_profile("hades_chxxnnx"):
            for source, target, expected in cases:
                with self.subTest(source=source, target=target):
                    once = _apply_source_aware_corrections(source, target)
                    twice = _apply_source_aware_corrections(source, once)
                    self.assertEqual(once, expected)
                    self.assertEqual(twice, once)

    def test_streamer_name_rendering_fixes_mixed_canonical_and_wrong_forms(self):
        cases = (
            (
                "챈나가 왔어요",
                "-chan ... Chxxnnx ... -chan",
                "Chxxnnx ... Chxxnnx ... Chxxnnx",
                ("-chan", "-Chan", "챈나"),
            ),
            (
                "봉준이 왔어요",
                "Bongjun ... Kim Bongjun",
                "Kim Bongjun ... Kim Bongjun",
                ("Kim Kim Bongjun", "Kim BongjunKim Bongjun"),
            ),
            (
                "성태는 왔어요",
                "Sungtae哥 ... KimSungtae",
                "KimSungtae ... KimSungtae",
                ("KimKimSungtae", "KimSungtaeKimSungtae"),
            ),
            (
                "챈나가 왔어요",
                "챈나 ... Chxxnnx ... 챈나",
                "Chxxnnx ... Chxxnnx ... Chxxnnx",
                ("챈나", "ChxxnnxChxxnnx"),
            ),
        )

        with _active_translation_profile("hades_chxxnnx"):
            for source, target, expected, forbidden_fragments in cases:
                with self.subTest(source=source, target=target):
                    once = _apply_source_aware_corrections(source, target)
                    twice = _apply_source_aware_corrections(source, once)
                    self.assertEqual(once, expected)
                    self.assertEqual(twice, once)
                    for fragment in forbidden_fragments:
                        self.assertNotIn(fragment, once)

    def test_streamer_name_rendering_already_canonical_only_is_stable(self):
        cases = (
            ("챈나가 왔어요", "Chxxnnx"),
            ("봉준이 왔어요", "Kim Bongjun"),
            ("성태는 왔어요", "KimSungtae"),
        )

        with _active_translation_profile("hades_chxxnnx"):
            for source, target in cases:
                with self.subTest(source=source, target=target):
                    once = _apply_source_aware_corrections(source, target)
                    twice = _apply_source_aware_corrections(source, once)
                    self.assertEqual(once, target)
                    self.assertEqual(twice, once)

    def test_streamer_name_rendering_repeated_correction_has_no_artifacts(self):
        cases = (
            (
                "챈나가 왔어요",
                "Chxxnnx好可愛",
                "Chxxnnx好可愛",
                ("ChxxnnxChxxnnx",),
            ),
            (
                "봉준이 왔어요",
                "Kim Bongjun是個人",
                "Kim Bongjun是個人",
                ("Kim Kim Bongjun", "Kim BongjunKim Bongjun"),
            ),
            (
                "봉준이 왔어요",
                "Bongjun + Kim Bongjun + Bongjun",
                "Kim Bongjun + Kim Bongjun + Kim Bongjun",
                ("Kim Kim Bongjun", "Kim BongjunKim Bongjun"),
            ),
            (
                "성태는 왔어요",
                "KimSungtae是老師",
                "KimSungtae是老師",
                ("KimKimSungtae", "KimSungtaeKimSungtae"),
            ),
            (
                "성태는 왔어요",
                "Sungtae哥 + KimSungtae",
                "KimSungtae + KimSungtae",
                ("KimKimSungtae", "KimSungtaeKimSungtae"),
            ),
        )

        with _active_translation_profile("hades_chxxnnx"):
            for source, target, expected, forbidden_fragments in cases:
                with self.subTest(source=source, target=target):
                    once = _apply_source_aware_corrections(source, target)
                    twice = _apply_source_aware_corrections(source, once)
                    self.assertEqual(once, expected)
                    self.assertEqual(twice, once)
                    for fragment in forbidden_fragments:
                        self.assertNotIn(fragment, once)

    def test_streamer_name_rendering_mixed_forms_remain_source_and_profile_gated(self):
        with _active_translation_profile("hades_chxxnnx"):
            target = "-chan ... Chxxnnx ... -chan"
            self.assertEqual(
                _apply_source_aware_corrections("오늘 방송 재미있다", target),
                target,
            )

        for profile_id in ("", "stellive_hina", "isegye_lilpa"):
            with self.subTest(profile_id=profile_id):
                with _active_translation_profile(profile_id):
                    target = "Bongjun ... Kim Bongjun"
                    self.assertEqual(
                        _apply_source_aware_corrections("봉준이 왔어요", target),
                        target,
                    )

        with _active_translation_profile("hades_chxxnnx", use_profile=False):
            target = "Sungtae哥 ... KimSungtae"
            self.assertEqual(
                _apply_source_aware_corrections("성태는 왔어요", target),
                target,
            )

    def test_streamer_name_rendering_cache_round_trip_does_not_double_apply(self):
        t = _make_translator()
        source = "봉준이 왔어요"
        with _active_translation_profile("hades_chxxnnx"):
            t._engines[0].translate.return_value = "Bongjun"

            first = t.translate_event(source)
            t._policy_state().reset_last_input()
            second = t.translate_event(source)

        self.assertEqual(first.result_source, "api")
        self.assertEqual(first.target_text, "Kim Bongjun")
        self.assertEqual(second.result_source, "memory_hit")
        self.assertEqual(second.target_text, "Kim Bongjun")
        self.assertEqual(t._engines[0].translate.call_count, 1)

    def test_translation_memory_honors_context_window_config(self):
        from config import cfg

        original = cfg.translation.context_window
        object.__setattr__(cfg.translation, "context_window", 10)
        try:
            memory = _new_translation_memory()
        finally:
            object.__setattr__(cfg.translation, "context_window", original)

        self.assertEqual(memory.recent.maxlen, 10)

    def test_prompt_version_uses_effective_engine_prompt(self):
        from config import cfg

        t = _make_translator()
        groq = _mock_engine("groq")
        original_compact = cfg.translation.groq_translation_compact_prompt
        original_profile = cfg.translation.use_profile
        object.__setattr__(cfg.translation, "groq_translation_compact_prompt", True)
        object.__setattr__(cfg.translation, "use_profile", False)
        try:
            base_hash = t._prompt_version("FULL PRIMARY PROMPT")
            groq_hash = t._prompt_version_for_engine(groq, "FULL PRIMARY PROMPT")
        finally:
            object.__setattr__(cfg.translation, "groq_translation_compact_prompt", original_compact)
            object.__setattr__(cfg.translation, "use_profile", original_profile)

        self.assertNotEqual(base_hash, groq_hash)

    def test_token_usage_is_omitted_when_usage_engine_differs_from_outcome(self):
        from modules.translation_engines import _log_token_usage, reset_last_token_usage

        reset_last_token_usage()
        _log_token_usage("nvidia", {"prompt_tokens": 99, "completion_tokens": 1})
        outcome = TranslationOutcome(
            source_text="source",
            target_text="target",
            status="success",
            result_source="api",
            cache_status="miss",
            incomplete=False,
            engine="groq",
            model="fallback-model",
        )

        self.assertEqual(_token_usage_for_outcome(outcome), {})

    def test_hot_path_rejection_reason_is_computed_once(self):
        t = _make_translator()
        t._engines[0].translate.return_value = "你好"
        original = t._policy.rejection_reason
        t._policy.rejection_reason = MagicMock(side_effect=original)

        outcome = t.translate_event("안녕하세요")

        self.assertEqual(outcome.status, "success")
        self.assertEqual(t._policy.rejection_reason.call_count, 1)

    def test_profile_prompt_version_changes_between_profiles(self):
        t = _make_translator()

        with _active_translation_profile("hades_chxxnnx"):
            hades_prompt_version = t._get_prompt_version_hash()
        with _active_translation_profile("stellive_hina"):
            stellive_prompt_version = t._get_prompt_version_hash()

        self.assertNotEqual(hades_prompt_version, stellive_prompt_version)

    def test_meta_garbage_detector_matches_explanatory_output(self):
        self.assertTrue(_looks_like_meta_garbage_output("（無法理解的STT亂碼，無明確語義）"))
        self.assertTrue(_looks_like_meta_garbage_output("（無意義詞，省略）"))
        self.assertFalse(_looks_like_meta_garbage_output("這句我無法理解你的心情。"))

    def test_single_word_explanatory_output_is_filtered(self):
        t = _make_translator()
        t._engines[0].translate.return_value = "（無意義詞，省略）"

        outcome = t.translate_event("글랜스")

        self.assertEqual(outcome.status, "filtered")
        self.assertEqual(outcome.result_source, "post_policy")
        self.assertEqual(outcome.filter_reason, "meta_garbage_output")
        self.assertIsNone(outcome.target_text)

    def test_cache_hit_on_second_call(self):
        t = _make_translator()
        t._engines[0].translate.return_value = "你好"
        t.translate("안녕하세요")
        t.translate("안녕하세요")   # second call — should hit cache
        total_calls = sum(e.translate.call_count for e in t._engines)
        self.assertEqual(total_calls, 1, "API should be called only once; second call hits cache")

    def test_cache_evicts_when_full(self):
        from modules.translator import _CACHE_MAX_SIZE
        t = _make_translator()
        engine = t._active_engine()
        prompt_ver = t._get_prompt_version_hash()
        for i in range(_CACHE_MAX_SIZE):
            t._memory.cache[
                (f"key{i}", False, prompt_ver, engine.engine_name, engine.model_name)
            ] = f"val{i}"
        t._memory.cache_store("overflow", False, "x", prompt_ver, engine)
        self.assertLessEqual(len(t._memory.cache), _CACHE_MAX_SIZE)
        self.assertIn(
            ("overflow", False, prompt_ver, engine.engine_name, engine.model_name),
            t._memory.cache,
        )

    def test_incomplete_lookup_skips_db(self):
        t = _make_translator()
        t._memory.db_lookup = MagicMock(return_value="DB result")
        lookup = t._lookup_existing_translation_event(
            "불완전한 문장", True, t._get_prompt_version_hash()
        )
        self.assertIsNone(lookup.result)
        self.assertEqual(lookup.source, "skipped")
        t._memory.db_lookup.assert_not_called()


class TestMaxTranslateCharsGuard(unittest.TestCase):
    """#6 — oversized inputs must be rejected before reaching any engine."""

    def _make_with_cap(self, cap: int = 10) -> Translator:
        t = _make_translator()
        # Replace the auto-built policy with one whose cap we control.
        from modules.translation_policy import TranslationPolicy
        from config import cfg
        t._policy = TranslationPolicy(
            slang=cfg.translation.slang,
            min_translate_chars=2,
            max_translate_chars=cap,
        )
        return t

    def test_too_long_skips_engine_call(self):
        t = self._make_with_cap(cap=10)
        outcome = t.translate_event("x" * 50, incomplete=False)
        self.assertEqual(outcome.status, "filtered")
        self.assertEqual(outcome.filter_reason, "too_long")
        self.assertIsNone(outcome.target_text)
        for e in t._engines:
            e.translate.assert_not_called()

    def test_within_cap_reaches_engine(self):
        t = self._make_with_cap(cap=100)
        outcome = t.translate_event("안녕하세요", incomplete=False)
        # Engine returns "你好" by default per _mock_engine
        self.assertEqual(outcome.status, "success")
        self.assertEqual(outcome.target_text, "你好")
        self.assertEqual(outcome.filter_reason, "")


class TestSttTemplateGarbageGuard(unittest.TestCase):
    """#8 — STT template hallucinations must be rejected before any engine,
    memory lookup or DB write."""

    _TEMPLATE = "시청해주셔서 감사합니다."

    def test_stt_template_garbage_skips_engine_call(self):
        t = _make_translator()

        outcome = t.translate_event(self._TEMPLATE, incomplete=False)

        self.assertEqual(outcome.status, "filtered")
        self.assertEqual(outcome.result_source, "policy")
        self.assertEqual(outcome.filter_reason, "stt_template_garbage")
        self.assertIsNone(outcome.target_text)
        for e in t._engines:
            e.translate.assert_not_called()

    def test_subscribe_cta_template_skips_engine_call(self):
        t = _make_translator()

        outcome = t.translate_event("구독과 좋아요 부탁드립니다!", incomplete=False)

        self.assertEqual(outcome.status, "filtered")
        self.assertEqual(outcome.result_source, "policy")
        self.assertEqual(outcome.filter_reason, "stt_template_garbage")
        self.assertIsNone(outcome.target_text)
        for e in t._engines:
            e.translate.assert_not_called()

    def test_stt_template_garbage_not_written_to_memory_or_db(self):
        t = _make_translator()
        t._record_success = MagicMock()

        t.translate_event(self._TEMPLATE, incomplete=False)

        t._record_success.assert_not_called()

    def test_stt_template_garbage_outcome_exposes_filter_reason(self):
        # runtime_events writes whatever `filter_reason` the outcome carries
        # via as_event_fields (same path as the `too_long` precedent) —
        # verify it is observable in the emitted event payload.
        t = _make_translator()

        outcome = t.translate_event(self._TEMPLATE, incomplete=False)
        event = outcome.as_event_fields(0.0, {})

        self.assertEqual(event["status"], "filtered")
        self.assertEqual(event["filter_reason"], "stt_template_garbage")


class TestSttTemplateFragmentSanitizer(unittest.TestCase):
    """#9 — boundary template fragments are stripped before translation."""

    _RAW = "시청해주셔서 감사합니다. 엄청나게 그렇잖아."
    _SANITIZED = "엄청나게 그렇잖아."

    def test_sanitizer_engine_receives_sanitized_text(self):
        t = _make_translator()

        t.translate_event(self._RAW, incomplete=False)

        self.assertEqual(t._engines[0].translate.call_args.args[0], self._SANITIZED)
        t._engines[1].translate.assert_not_called()

    def test_sanitizer_db_stores_sanitized_key(self):
        t = _make_translator()
        t._record_success = MagicMock()

        t.translate_event(self._RAW, incomplete=False)

        t._record_success.assert_called_once()
        self.assertEqual(t._record_success.call_args.args[0], self._SANITIZED)

    def test_sanitizer_outcome_source_text_is_original(self):
        t = _make_translator()

        outcome = t.translate_event(self._RAW, incomplete=False)

        self.assertEqual(outcome.status, "success")
        self.assertEqual(outcome.source_text, self._RAW)
        self.assertEqual(t._engines[0].translate.call_args.args[0], self._SANITIZED)

    def test_sanitized_to_empty_has_expected_filter_reason(self):
        t = _make_translator()

        outcome = t.translate_event("시청해주셔서 감사합니다. abcde!", incomplete=False)

        self.assertEqual(outcome.status, "filtered")
        self.assertEqual(outcome.result_source, "policy")
        self.assertEqual(outcome.filter_reason, "stt_sanitized_empty")
        for e in t._engines:
            e.translate.assert_not_called()

    def test_hard_template_tail_engine_receives_sanitized_text(self):
        t = _make_translator()
        raw = (
            "입주비는 안 받습니다. 저희 스폰서분들도 너무 감사하게도 좀 많이 붙어가지고. "
            "자막 제공 및 자막 제공 및 광고를 포함하고 있습니다."
        )
        sanitized = "입주비는 안 받습니다. 저희 스폰서분들도 너무 감사하게도 좀 많이 붙어가지고"

        outcome = t.translate_event(raw, incomplete=False)

        self.assertEqual(outcome.status, "success")
        self.assertEqual(outcome.source_text, raw)
        self.assertEqual(t._engines[0].translate.call_args.args[0], sanitized)

    def test_subscribe_cta_prefix_engine_receives_sanitized_text(self):
        with _active_translation_profile("hades_chxxnnx"):
            t = _make_translator()
            raw = "구독과 좋아요 부탁 어? 진짜? 카페에 챗나룩 서버 포스터 누가 큐티 버전으로 올려주셨다고요?"
            sanitized = "어? 진짜? 카페에 챈나룩 서버 포스터 누가 큐티 버전으로 올려주셨다고요?"

            outcome = t.translate_event(raw, incomplete=False)

            self.assertEqual(outcome.status, "success")
            self.assertEqual(outcome.source_text, raw)
            self.assertEqual(t._engines[0].translate.call_args.args[0], sanitized)

    def test_subscribe_topic_prefix_engine_receives_sanitized_text(self):
        t = _make_translator()
        raw = "구독과 좋아요는 저 이제 2집 녹음하러 가거든요? 여러분들?"
        sanitized = "저 이제 2집 녹음하러 가거든요? 여러분들?"

        outcome = t.translate_event(raw, incomplete=False)

        self.assertEqual(outcome.status, "success")
        self.assertEqual(outcome.source_text, raw)
        self.assertEqual(t._engines[0].translate.call_args.args[0], sanitized)

    def test_hard_template_prefix_engine_receives_sanitized_text(self):
        t = _make_translator()
        raw = "자막 제공 및 자막 제공 및 광고를 포함하고 있습니다. 우와! 너무 예쁘던데?"
        sanitized = "우와! 너무 예쁘던데?"

        outcome = t.translate_event(raw, incomplete=False)

        self.assertEqual(outcome.status, "success")
        self.assertEqual(outcome.source_text, raw)
        self.assertEqual(t._engines[0].translate.call_args.args[0], sanitized)


class TestSttLowValueFragmentGuard(unittest.TestCase):
    def test_low_value_fragment_skips_engine_call(self):
        t = _make_translator()

        outcome = t.translate_event("도도리코 소라에 타받세 도개가 사라지게 된 날", incomplete=False)

        self.assertEqual(outcome.status, "filtered")
        self.assertEqual(outcome.result_source, "policy")
        self.assertEqual(outcome.filter_reason, "stt_low_value_fragment")
        self.assertIsNone(outcome.target_text)
        for e in t._engines:
            e.translate.assert_not_called()

    def test_low_value_tail_engine_receives_sanitized_text(self):
        t = _make_translator()
        raw = "그걸 직접 만들 수 있다고? 너무 기대돼 망간부 바카스탕 골라요"
        sanitized = "그걸 직접 만들 수 있다고? 너무 기대돼"

        outcome = t.translate_event(raw, incomplete=False)

        self.assertEqual(outcome.status, "success")
        self.assertEqual(outcome.source_text, raw)
        self.assertEqual(t._engines[0].translate.call_args.args[0], sanitized)


class TestSttSongFragmentGuard(unittest.TestCase):
    def test_song_fragment_skips_engine_call(self):
        t = _make_translator()

        outcome = t.translate_event(
            "쓰읍... 락이라면서 띵시렁띵시렁 흐흐흐 나는 아름다운 남의 날개를",
            incomplete=False,
        )

        self.assertEqual(outcome.status, "filtered")
        self.assertEqual(outcome.result_source, "policy")
        self.assertEqual(outcome.filter_reason, "stt_song_fragment")
        self.assertIsNone(outcome.target_text)
        for e in t._engines:
            e.translate.assert_not_called()


class TestSourceNormBeforeMatching(unittest.TestCase):
    def test_stellive_hina_profile_normalizes_runtime_variants(self):
        raw = "히나유키 히나랑 해동이 일기생이 투리버스 메들린 얘기했어"

        with _active_translation_profile("stellive_hina"):
            self.assertEqual(
                _normalize_source_before_matching(raw),
                "시라유키 히나랑 해둥이 1기생이 투니버스 메들리 얘기했어",
            )

    def test_stellive_hina_norm_is_profile_and_use_profile_gated(self):
        raw = "히나유키 히나랑 해동이 일기생"

        for profile_id in ("", "hades_chxxnnx", "mwmeu", "isegye_lilpa"):
            with self.subTest(profile_id=profile_id):
                with _active_translation_profile(profile_id):
                    self.assertEqual(_normalize_source_before_matching(raw), raw)

        with _active_translation_profile("stellive_hina", use_profile=False):
            self.assertEqual(_normalize_source_before_matching(raw), raw)

    def test_hades_profile_normalizes_mixed_script(self):
        with _active_translation_profile("hades_chxxnnx"):
            self.assertEqual(_normalize_source_before_matching("服주 화이팅"), "섭주 화이팅")
            self.assertEqual(_normalize_source_before_matching("서버 服주입니다"), "서버 섭주입니다")
            self.assertEqual(_normalize_source_before_matching("服주"), "섭주")

    def test_hades_use_profile_false_no_change(self):
        with _active_translation_profile("hades_chxxnnx", use_profile=False):
            self.assertEqual(_normalize_source_before_matching("服주 화이팅"), "服주 화이팅")

    def test_other_profiles_no_change(self):
        for profile_id in ("stellive_hina", "isegye_lilpa", ""):
            with self.subTest(profile_id=profile_id):
                with _active_translation_profile(profile_id):
                    self.assertEqual(
                        _normalize_source_before_matching("服주 화이팅"), "服주 화이팅"
                    )

    def test_seop_jeong_unchanged_in_all_profiles(self):
        for profile_id in ("hades_chxxnnx", "stellive_hina", "isegye_lilpa", ""):
            with self.subTest(profile_id=profile_id):
                with _active_translation_profile(profile_id):
                    self.assertEqual(
                        _normalize_source_before_matching("섭정 있는 거야?"),
                        "섭정 있는 거야?",
                    )

    def test_existing_slang_variants_pass_through(self):
        with _active_translation_profile("hades_chxxnnx"):
            self.assertEqual(_normalize_source_before_matching("섭쥬 화이팅"), "섭쥬 화이팅")
            self.assertEqual(_normalize_source_before_matching("썹주 화이팅"), "썹주 화이팅")
            self.assertEqual(_normalize_source_before_matching("SUBJU"), "SUBJU")
            self.assertEqual(_normalize_source_before_matching("服主"), "服主")

    def test_normalization_is_idempotent(self):
        with _active_translation_profile("hades_chxxnnx"):
            once = _normalize_source_before_matching("服주 화이팅")
            twice = _normalize_source_before_matching(once)
            self.assertEqual(once, "섭주 화이팅")
            self.assertEqual(twice, once)

    def test_hades_runtime_name_variants_normalized(self):
        cases = [
            ("김띵귤 왔어", "띵귤 왔어"),
            ("김챗나 방", "챈나 방"),
            ("김챔나 방", "챈나 방"),
            ("챗나야 빨리 와", "챈나야 빨리 와"),
            ("주먹이 왔어", "솜펀치 왔어"),
            ("주먹 언니 와요", "솜펀치 언니 와요"),
            ("팅귤이 왔어", "띵귤이 왔어"),
            ("틴귤아 와", "띵귤아 와"),
            ("큐마는 대기 중", "키마는 대기 중"),
            ("채엔나 방", "챈나 방"),
            ("차엔나 방", "챈나 방"),
        ]
        with _active_translation_profile("hades_chxxnnx"):
            for raw, expected in cases:
                with self.subTest(raw=raw):
                    once = _normalize_source_before_matching(raw)
                    self.assertEqual(once, expected)
                    self.assertEqual(_normalize_source_before_matching(once), once)

    def test_hades_채나_family_normalized(self):
        cases = [
            ("채나", "챈나"),
            ("채나야", "챈나야"),
            ("채나님", "챈나님"),
            ("채나로", "챈나로"),
            ("채나롱", "챈나롱"),
            ("채나룬", "챈나룬"),
            ("천사채나", "천사챈나"),
        ]
        with _active_translation_profile("hades_chxxnnx"):
            for raw, expected in cases:
                with self.subTest(raw=raw):
                    self.assertEqual(_normalize_source_before_matching(raw), expected)

    def test_채나_compound_no_double_replace(self):
        with _active_translation_profile("hades_chxxnnx"):
            self.assertEqual(_normalize_source_before_matching("천사채나"), "천사챈나")

    def test_채나_norm_is_profile_gated(self):
        for profile_id in ("stellive_hina", "isegye_lilpa", ""):
            with self.subTest(profile_id=profile_id):
                with _active_translation_profile(profile_id):
                    self.assertEqual(_normalize_source_before_matching("채나"), "채나")

    def test_채나_norm_is_use_profile_gated(self):
        with _active_translation_profile("hades_chxxnnx", use_profile=False):
            self.assertEqual(_normalize_source_before_matching("채나"), "채나")

    def test_mwmeu_profile_normalizes_runtime_name_and_fandom_variants(self):
        raw = "이변이한테 초운이 집에 가야 되고 조은아 엔즈들이 기다려. 이츠가 소아가 지안 언니랑 왔어."

        with _active_translation_profile("mwmeu"):
            self.assertEqual(
                _normalize_source_before_matching(raw),
                "이비한테 초은이 집에 가야 되고 초은아 웬즈들이 기다려. 리츠가 수아가 지한 언니랑 왔어.",
            )

    def test_mwmeu_profile_normalizes_current_stream_game_variants(self):
        raw = (
            "오마쿡스 바로 투 하자. 플러스 인제 생빠이니까 토화기 들고 "
            "명예 소변관 해. 나는 이 가나디아인데."
        )

        with _active_translation_profile("mwmeu"):
            self.assertEqual(
                _normalize_source_before_matching(raw),
                "오버쿡드 2 하자. 플러스 이제 선배니까 소화기 들고 "
                "명예 소방관 해. 나는 이 강아지인데.",
            )

    def test_mwmeu_profile_normalizes_chiikawa_runtime_variants(self):
        raw = "시에가파크 갔다가 시가와 굿즈랑 하치와래랑 모몽가를 봤어."

        with _active_translation_profile("mwmeu"):
            self.assertEqual(
                _normalize_source_before_matching(raw),
                "치이카와파크 갔다가 치이카와 굿즈랑 하치와레랑 모몽가를 봤어.",
            )

    def test_mwmeu_norm_is_profile_and_use_profile_gated(self):
        raw = "이변이한테 초운이랑 시에가파크 얘기했어"

        for profile_id in ("hades_chxxnnx", "stellive_hina", "isegye_lilpa", ""):
            with self.subTest(profile_id=profile_id):
                with _active_translation_profile(profile_id):
                    self.assertEqual(_normalize_source_before_matching(raw), raw)

        with _active_translation_profile("mwmeu", use_profile=False):
            self.assertEqual(_normalize_source_before_matching(raw), raw)


class TestSourceNormIntegration(unittest.TestCase):
    def test_standalone_hanja_hangul_hits_slang(self):
        """translate_event("服주") under HADES → slang hit, engine not called, raw source preserved."""
        with _active_translation_profile("hades_chxxnnx"):
            t = _make_translator()
            outcome = t.translate_event("服주")
            self.assertEqual(outcome.status, "success")
            self.assertEqual(outcome.result_source, "slang")
            self.assertEqual(outcome.target_text, "服主")
            self.assertEqual(outcome.source_text, "服주")
            for e in t._engines:
                e.translate.assert_not_called()

    def test_mid_sentence_engine_receives_normalized_text(self):
        """translate_event mid-sentence → engine called with 섭주, source_text stays raw."""
        with _active_translation_profile("hades_chxxnnx"):
            t = _make_translator()
            t._engines[0].translate.return_value = "伺服器 服主 加油"
            outcome = t.translate_event("서버 服주 화이팅")
            self.assertEqual(outcome.source_text, "서버 服주 화이팅")
            t._engines[0].translate.assert_called_once()
            call_text = t._engines[0].translate.call_args[0][0]
            self.assertEqual(call_text, "서버 섭주 화이팅")

    def test_wrong_profile_engine_receives_raw_text(self):
        """Wrong profile → no normalization → engine sees raw 服주."""
        with _active_translation_profile("stellive_hina"):
            t = _make_translator()
            t.translate_event("服주 화이팅")
            call_text = t._engines[0].translate.call_args[0][0]
            self.assertIn("服주", call_text)

    def test_use_profile_false_engine_receives_raw_text(self):
        """use_profile=False → no normalization → engine sees raw 服주."""
        with _active_translation_profile("hades_chxxnnx", use_profile=False):
            t = _make_translator()
            t.translate_event("服주 화이팅")
            call_text = t._engines[0].translate.call_args[0][0]
            self.assertIn("服주", call_text)

    def test_seop_jeong_engine_receives_unchanged_source(self):
        """섭정 under HADES → engine receives unchanged Korean source."""
        with _active_translation_profile("hades_chxxnnx"):
            t = _make_translator()
            t.translate_event("섭정 있는 거야?")
            call_text = t._engines[0].translate.call_args[0][0]
            self.assertEqual(call_text, "섭정 있는 거야?")

    def test_mwmeu_runtime_variants_engine_receives_normalized_text(self):
        with _active_translation_profile("mwmeu"):
            t = _make_translator()
            t._engines[0].translate.return_value = "初雲和千川"

            outcome = t.translate_event("초운이 시에가파크 갔어")

            self.assertEqual(outcome.source_text, "초운이 시에가파크 갔어")
            self.assertEqual(outcome.target_text, "초은和Chiikawa")
            call_text = t._engines[0].translate.call_args[0][0]
            self.assertEqual(call_text, "초은이 치이카와파크 갔어")

    def test_mwmeu_current_stream_variants_engine_receives_normalized_text(self):
        with _active_translation_profile("mwmeu"):
            t = _make_translator()
            t._engines[0].translate.return_value = "Oma-kooks 立刻投！投！"

            outcome = t.translate_event("오마쿡스 바로 투 할게")

            self.assertEqual(outcome.source_text, "오마쿡스 바로 투 할게")
            self.assertEqual(outcome.target_text, "《胡鬧廚房2》")
            call_text = t._engines[0].translate.call_args[0][0]
            self.assertEqual(call_text, "오버쿡드 2 할게")


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# H1 regression: a malformed queue item must not stall the in-order emit loop
# ---------------------------------------------------------------------------

class TestMalformedItemDoesNotStallEmit(unittest.TestCase):

    def test_bad_item_then_good_item_still_emits(self):
        from modules.translator import start as translator_start

        class _Boom:
            def __str__(self):
                raise RuntimeError("malformed pipeline item")

        sentence_q: queue.Queue = queue.Queue()
        subtitle_q: queue.Queue = queue.Queue()
        stop = threading.Event()
        engine = _mock_engine("primary", "\u4f60\u597d")

        with patch.object(translator_module, "_build_engine_chain", return_value=[engine]), \
                patch.object(translator_module, "runtime_events") as events:
            translator_start(sentence_q, subtitle_q, stop)
            sentence_q.put(_Boom())   # seq 0 — translate_item raises on sentence_text
            sentence_q.put({"text": "\uc548\ub155\ud558\uc138\uc694 \uc624\ub298 \ubc29\uc1a1 \uc2dc\uc791\ud569\ub2c8\ub2e4", "incomplete": False})

            deadline = time.monotonic() + 5.0
            result = None
            while time.monotonic() < deadline:
                try:
                    result = subtitle_q.get(timeout=0.1)
                    break
                except queue.Empty:
                    continue
            stop.set()

        self.assertEqual(result, "\u4f60\u597d", "good item after malformed item must still emit")
        emitted = [call.kwargs for call in events.emit.call_args_list]
        self.assertGreaterEqual(len(emitted), 2)
        self.assertTrue(all(event["translation_mode"] == "live" for event in emitted))


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# P2: DB cache layer gating by translation mode
# ---------------------------------------------------------------------------

class TestDbCacheGating(unittest.TestCase):
    """Live mode skips the SQLite cache (0.45% measured hit rate); clip keeps it."""

    def _translator_with_db_spies(self):
        t = _make_translator()
        t._engines[0].translate.return_value = "你好"
        t._memory.db_lookup = MagicMock(return_value=None)
        t._memory.db_store = MagicMock()
        return t

    def _set_mode(self, mode):
        from config import cfg
        original = cfg.translation.translation_mode
        object.__setattr__(cfg.translation, "translation_mode", mode)
        return original

    def test_live_mode_skips_db_lookup_and_store(self):
        original = self._set_mode("live")
        try:
            t = self._translator_with_db_spies()
            outcome = t.translate_event("안녕하세요 방송 시작합니다")
            self.assertEqual(outcome.status, "success")
            t._memory.db_lookup.assert_not_called()
            t._memory.db_store.assert_not_called()
        finally:
            self._set_mode(original)

    def test_clip_mode_uses_db_cache(self):
        original = self._set_mode("clip")
        try:
            t = self._translator_with_db_spies()
            outcome = t.translate_event("안녕하세요 방송 시작합니다")
            self.assertEqual(outcome.status, "success")
            t._memory.db_lookup.assert_called_once()
            t._memory.db_store.assert_called_once()
        finally:
            self._set_mode(original)

    def test_live_mode_flag_reenables_db_cache(self):
        from config import cfg
        original = self._set_mode("live")
        original_flag = cfg.database.live_db_cache
        object.__setattr__(cfg.database, "live_db_cache", True)
        try:
            t = self._translator_with_db_spies()
            t.translate_event("안녕하세요 방송 시작합니다")
            t._memory.db_lookup.assert_called_once()
            t._memory.db_store.assert_called_once()
        finally:
            object.__setattr__(cfg.database, "live_db_cache", original_flag)
            self._set_mode(original)
