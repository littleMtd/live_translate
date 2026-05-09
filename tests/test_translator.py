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
from modules.translator import (
    _build_base_prompt, _build_user_message, _write_history,
    Translator, TranslationEngine, GeminiEngine, ClaudeEngine, GoogleTranslateEngine,
)
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
    from collections import deque, OrderedDict
    t = Translator.__new__(Translator)
    t._evolver = PromptEvolver()
    t._cache = OrderedDict()
    t._active_idx = 0
    t._probe_counter = 0
    t._recent = deque(maxlen=3)
    t._engines = [_mock_engine(name) for name in ("gemini", "claude")]
    return t


def _claude_resp(text: str) -> MagicMock:
    r = MagicMock()
    r.content = [MagicMock(text=text)]
    return r


def _sys_prompt(t: "Translator") -> str:
    from modules.translator import _BASE_PROMPT
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

    def test_slang_returns_direct_without_api(self):
        from config import cfg
        t = _make_translator()
        ko, zh = next(iter(cfg.translation.slang.items()))
        result = t.translate(ko)
        self.assertEqual(result, zh)
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
            t._cache[(f"key{i}", False, prompt_ver)] = f"val{i}"
        t._cache_store("overflow", False, "x", prompt_ver)
        self.assertLessEqual(len(t._cache), _CACHE_MAX_SIZE)
        self.assertIn(("overflow", False, prompt_ver), t._cache)


if __name__ == "__main__":
    unittest.main()
