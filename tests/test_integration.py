"""
Integration tests — verify inter-thread queue plumbing and real API contracts.

Mock tests:   always run (no API keys needed)
Real API tests: skipped unless the corresponding env var is set
"""
import os
import queue
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import pytest
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

# Stub sounddevice so audio_capture can be imported on CI (no audio hardware)
if "sounddevice" not in sys.modules:
    sys.modules["sounddevice"] = MagicMock()

import modules.sentence_splitter as sentence_splitter
import modules.translator as translator
from contextlib import contextmanager
from config import cfg

_LIVE_TESTS_ENABLED = os.getenv("RUN_LIVE_TESTS") == "1"


class _NoOpDB:
    @property
    def available(self): return False
    def lookup(self, *a, **kw): return None
    def store(self, *a, **kw): pass
    def close(self): pass


@contextmanager
def _mock_primary(text: str):
    """Mock the entire engine chain so the translator returns `text` for any input."""
    from modules.translator import TranslationEngine
    mock_engine = MagicMock(spec=TranslationEngine)
    mock_engine.engine_name = "mock"
    mock_engine.model_name = "mock-model"
    mock_engine.available = True
    mock_engine.translate.return_value = text or None
    with patch("modules.translator._build_engine_chain", return_value=[mock_engine]), \
         patch("modules.translator._get_db", return_value=_NoOpDB()):
        yield


# ---------------------------------------------------------------------------
# Queue / threading pipeline tests  (no real API calls)
# ---------------------------------------------------------------------------

class TestSplitterThread(unittest.TestCase):
    """Verify sentence_splitter thread feeds sentence_queue correctly."""

    def _run(self, tokens: list[str], timeout: float = 6.0) -> dict:
        text_q = queue.Queue()
        sentence_q = queue.Queue()
        stop = threading.Event()

        t = sentence_splitter.start(text_q, sentence_q, stop)
        for tok in tokens:
            text_q.put(tok)
        try:
            return sentence_q.get(timeout=timeout)
        finally:
            stop.set()
            t.join(timeout=2)

    def test_complete_sentence_flows_through(self):
        result = self._run(["안녕하세요"])
        self.assertEqual(result["text"], "안녕하세요")
        self.assertFalse(result["incomplete"])

    def test_multiple_tokens_are_joined(self):
        result = self._run(["진짜", "대박이에요"])
        self.assertIn("진짜", result["text"])
        self.assertIn("대박이에요", result["text"])

    def test_stop_event_exits_cleanly(self):
        text_q = queue.Queue()
        sentence_q = queue.Queue()
        stop = threading.Event()

        t = sentence_splitter.start(text_q, sentence_q, stop)
        stop.set()
        t.join(timeout=3)
        self.assertFalse(t.is_alive())


class TestTranslatorThread(unittest.TestCase):
    """Verify translator thread reads sentence_queue and writes subtitle_queue."""

    def test_sentence_reaches_subtitle_queue(self):
        sentence_q = queue.Queue()
        subtitle_q = queue.Queue()
        stop = threading.Event()

        with _mock_primary("你好"):
            t = translator.start(sentence_q, subtitle_q, stop)
            sentence_q.put({"text": "안녕하세요", "incomplete": False})
            result = subtitle_q.get(timeout=5)
            stop.set()
            t.join(timeout=2)

        self.assertEqual(result, "你好")

    def test_stop_event_exits_cleanly(self):
        sentence_q = queue.Queue()
        subtitle_q = queue.Queue()
        stop = threading.Event()

        with _mock_primary(""):
            t = translator.start(sentence_q, subtitle_q, stop)
            stop.set()
            t.join(timeout=3)

        self.assertFalse(t.is_alive())


class TestFullMockPipeline(unittest.TestCase):
    """End-to-end: text tokens → splitter → translator → subtitle."""

    def test_text_token_arrives_as_subtitle(self):
        text_q = queue.Queue()
        sentence_q = queue.Queue()
        subtitle_q = queue.Queue()
        stop = threading.Event()

        with _mock_primary("歡迎來到直播"):
            t1 = sentence_splitter.start(text_q, sentence_q, stop)
            t2 = translator.start(sentence_q, subtitle_q, stop)
            text_q.put("환영합니다")  # 다 = complete ending
            result = subtitle_q.get(timeout=10)
            stop.set()
            t1.join(timeout=2)
            t2.join(timeout=2)

        self.assertEqual(result, "歡迎來到直播")

    def test_incomplete_sentence_still_translated(self):
        """Force-cut sentences (incomplete=True) still reach subtitle_queue."""
        sentence_q = queue.Queue()
        subtitle_q = queue.Queue()
        stop = threading.Event()

        with _mock_primary("正在遊戲中"):
            t = translator.start(sentence_q, subtitle_q, stop)
            sentence_q.put({"text": "게임 하고", "incomplete": True})
            result = subtitle_q.get(timeout=5)
            stop.set()
            t.join(timeout=2)

        self.assertEqual(result, "正在遊戲中")


# ---------------------------------------------------------------------------
# pause_event integration tests
# ---------------------------------------------------------------------------

class TestPauseEventIntegration(unittest.TestCase):

    def test_pause_blocks_splitter_output(self):
        text_q = queue.Queue()
        sentence_q = queue.Queue()
        stop = threading.Event()
        pause = threading.Event()
        pause.set()   # paused from the start

        t = sentence_splitter.start(text_q, sentence_q, stop, pause_event=pause)
        text_q.put("안녕하세요")   # complete ending
        time.sleep(0.5)
        stop.set()
        t.join(timeout=2)

        self.assertTrue(sentence_q.empty(), "Paused splitter must not emit")

    def test_pause_blocks_translator_output(self):
        sentence_q = queue.Queue()
        subtitle_q = queue.Queue()
        stop = threading.Event()
        pause = threading.Event()
        pause.set()

        with _mock_primary("你好"):
            t = translator.start(sentence_q, subtitle_q, stop, pause_event=pause)
            sentence_q.put({"text": "안녕하세요", "incomplete": False})
            time.sleep(0.5)
            stop.set()
            t.join(timeout=2)

        self.assertTrue(subtitle_q.empty(), "Paused translator must not emit")

    def test_full_pipeline_paused_then_resumed(self):
        """Resume after pause: post-resume token reaches subtitle_queue."""
        text_q = queue.Queue()
        sentence_q = queue.Queue()
        subtitle_q = queue.Queue()
        stop = threading.Event()
        pause = threading.Event()

        with _mock_primary("再見"):
            t1 = sentence_splitter.start(text_q, sentence_q, stop, pause_event=pause)
            t2 = translator.start(sentence_q, subtitle_q, stop, pause_event=pause)

            pause.set()
            text_q.put("안녕하세요")
            time.sleep(0.3)
            pause.clear()
            # Wait > 0.5 s (the splitter's inner-loop tick) so the drain on
            # unpause completes before we add the post-resume token.
            time.sleep(0.7)
            text_q.put("안녕히 가세요")   # 요 = complete

            result = subtitle_q.get(timeout=8)
            stop.set()
            t1.join(timeout=2)
            t2.join(timeout=2)

        self.assertEqual(result, "再見")


# ---------------------------------------------------------------------------
# Real API tests  (skipped in CI unless secrets are available)
# ---------------------------------------------------------------------------

@pytest.mark.live_api
@unittest.skipUnless(
    _LIVE_TESTS_ENABLED and os.getenv("ANTHROPIC_API_KEY"),
    "set RUN_LIVE_TESTS=1 and ANTHROPIC_API_KEY to run live Claude tests",
)
class TestRealClaude(unittest.TestCase):
    """Live Claude API calls — requires ANTHROPIC_API_KEY."""

    @classmethod
    def setUpClass(cls):
        import importlib
        # Other test files stub anthropic/google with MagicMock at load time.
        # importlib.import_module() checks sys.modules first, so we must pop()
        # the stub BEFORE importing the real package, then restore in tearDownClass
        # so subsequent mock-based tests still pass.
        cls._stubs: dict = {}
        for mod in ("google", "google.genai", "anthropic"):
            if isinstance(sys.modules.get(mod), MagicMock):
                cls._stubs[mod] = sys.modules.pop(mod)
                try:
                    sys.modules[mod] = importlib.import_module(mod)
                except ImportError:
                    raise unittest.SkipTest(f"{mod} not installed")
        from modules.translator import Translator
        cls.translator = Translator()

    @classmethod
    def tearDownClass(cls):
        # Restore stubs so subsequent mock-based tests (test_translator) still pass
        for mod, stub in cls._stubs.items():
            sys.modules[mod] = stub

    def test_translates_korean_greeting(self):
        result = self.translator.translate("안녕하세요", incomplete=False)
        self.assertIsNotNone(result)
        self.assertGreater(len(result), 0)

    def test_translates_slang(self):
        result = self.translator.translate("진짜 대박이에요 ㅋㅋ", incomplete=False)
        self.assertIsNotNone(result)
        self.assertGreater(len(result), 0)

    def test_incomplete_sentence_translated(self):
        result = self.translator.translate("지금 게임 하고", incomplete=True)
        self.assertIsNotNone(result)


@pytest.mark.live_api
@unittest.skipUnless(
    _LIVE_TESTS_ENABLED and os.getenv("GROQ_API_KEY"),
    "set RUN_LIVE_TESTS=1 and GROQ_API_KEY to run live Groq STT tests",
)
class TestRealGroqSTT(unittest.TestCase):
    """Live Groq STT call — requires GROQ_API_KEY and numpy."""

    @classmethod
    def setUpClass(cls):
        try:
            import numpy as np
            cls.np = np
        except ImportError:
            raise unittest.SkipTest("numpy not installed")
        from modules.stt import STTEngine
        cls.engine = STTEngine()

    def test_silent_audio_returns_none_or_empty(self):
        silent = self.np.zeros(16000, dtype=self.np.float32)
        result = self.engine.transcribe(silent)
        # Silence should not produce substantial output
        if result is not None:
            self.assertLess(len(result.strip()), 15,
                            "STT hallucinated text on silent audio")


if __name__ == "__main__":
    unittest.main()
