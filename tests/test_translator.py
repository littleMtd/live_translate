import sys

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
from modules.translation_engines import (
    _build_user_message, TranslationEngine, GeminiEngine, ClaudeEngine, GoogleTranslateEngine,
)
from modules.translation_prompts import (
    _BASE_PROMPT,
    _build_base_prompt,
    get_translation_profile,
    translation_profile_ids,
)
from modules.translator import _write_history, Translator
from modules.prompt_evolver import PromptEvolver
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
    t._evolver = PromptEvolver()
    t._active_idx = 0
    t._probe_counter = 0
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
    return t._evolver.build_system_prompt(_BASE_PROMPT)


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
# GeminiEngine unit tests
# ---------------------------------------------------------------------------

class TestGeminiEngine(unittest.TestCase):

    def _make_engine(self, resp_text: str = "你好", side_effect=None) -> GeminiEngine:
        e = GeminiEngine.__new__(GeminiEngine)
        e._client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.text = resp_text
        if side_effect:
            e._client.models.generate_content.side_effect = side_effect
        else:
            e._client.models.generate_content.return_value = mock_resp
        return e

    def test_returns_translated_text(self):
        e = self._make_engine("你好")
        self.assertEqual(e.translate("안녕하세요", _sys_prompt(_make_translator()), False), "你好")

    def test_strips_whitespace(self):
        e = self._make_engine("  你好  ")
        self.assertEqual(e.translate("안녕하세요", _sys_prompt(_make_translator()), False), "你好")

    def test_returns_none_on_exception(self):
        e = self._make_engine(side_effect=Exception("Gemini down"))
        self.assertIsNone(e.translate("안녕하세요", _sys_prompt(_make_translator()), False))

    def test_returns_none_when_client_is_none(self):
        e = GeminiEngine.__new__(GeminiEngine)
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

    def test_probe_restores_primary_after_recovery(self):
        from modules.translator import _FALLBACK_PROBE_EVERY
        t = _make_translator()
        t._active_idx = 1                            # currently on fallback
        t._probe_counter = _FALLBACK_PROBE_EVERY - 1 # one away from probe
        t._engines[0].translate.return_value = "你好"  # primary recovered
        t._engines[1].translate.return_value = "fallback"

        result = t.translate("안녕하세요")
        self.assertEqual(result, "你好")
        self.assertEqual(t._active_idx, 0, "Primary should be restored after successful probe")

    def test_probe_stays_on_fallback_if_primary_still_down(self):
        from modules.translator import _FALLBACK_PROBE_EVERY
        t = _make_translator()
        t._active_idx = 1
        t._probe_counter = _FALLBACK_PROBE_EVERY - 1
        t._engines[0].translate.return_value = None   # primary still down
        t._engines[1].translate.return_value = "fallback result"

        result = t.translate("안녕하세요")
        self.assertEqual(result, "fallback result")
        self.assertEqual(t._active_idx, 1, "Should stay on fallback if probe fails")

    def test_primary_fails_cascades_to_fallback(self):
        t = _make_translator()
        t._engines[0].translate.return_value = None  # engine returns None on failure
        t._engines[1].translate.return_value = "哈囉"

        result = t.translate("안녕하세요")
        self.assertEqual(result, "哈囉")
        self.assertEqual(t._active_idx, 1, "active_idx should advance after cascade")


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
        self.assertEqual(outcome.cache_status, "miss")
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
        prompt_ver = t._get_prompt_version_hash()
        for i in range(_CACHE_MAX_SIZE):
            t._memory.cache[(f"key{i}", False, prompt_ver)] = f"val{i}"
        t._memory.cache_store("overflow", False, "x", prompt_ver)
        self.assertLessEqual(len(t._memory.cache), _CACHE_MAX_SIZE)
        self.assertIn(("overflow", False, prompt_ver), t._memory.cache)

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


if __name__ == "__main__":
    unittest.main()
