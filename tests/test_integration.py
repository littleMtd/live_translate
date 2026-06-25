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
from modules.translator import TranslationOutcome
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
        yield mock_engine


@contextmanager
def _active_profile(profile_id: str, use_profile: bool = True):
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
        self.assertEqual(result.text, "안녕하세요")
        self.assertFalse(result.incomplete)

    def test_multiple_tokens_are_joined(self):
        result = self._run(["진짜", "대박이에요"])
        self.assertIn("진짜", result.text)
        self.assertIn("대박이에요", result.text)

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

    def test_translator_thread_emits_runtime_event(self):
        sentence_q = queue.Queue()
        subtitle_q = queue.Queue()
        stop = threading.Event()

        with _mock_primary("你好"), patch("modules.translator.runtime_events") as events:
            t = translator.start(sentence_q, subtitle_q, stop)
            sentence_q.put({"text": "안녕하세요", "incomplete": False, "stt_engine": "groq"})
            result = subtitle_q.get(timeout=5)
            stop.set()
            t.join(timeout=2)

        self.assertEqual(result, "你好")
        events.emit.assert_called_once()
        args, kwargs = events.emit.call_args
        self.assertEqual(args, ("translation",))
        self.assertEqual(kwargs["source_text"], "안녕하세요")
        self.assertEqual(kwargs["target_text"], "你好")
        self.assertTrue(kwargs["subtitle_emitted"])
        self.assertEqual(kwargs["stt_engine"], "groq")
        self.assertEqual(kwargs["sequence_id"], 0)
        self.assertIn("engine_latency_ms", kwargs)
        self.assertIn("queue_wait_ms", kwargs)
        self.assertIn("output_delay_ms", kwargs)
        self.assertIn("predecessor_stall_ms", kwargs)
        self.assertGreaterEqual(kwargs["output_delay_ms"], kwargs["engine_latency_ms"])
        self.assertEqual(kwargs["retry_count"], 0)
        self.assertEqual(kwargs["retry_reason"], "")
        self.assertFalse(kwargs["starts_with_dependency_marker"])
        self.assertEqual(kwargs["translation_mode"], "live")

    def test_translation_event_carries_corrections(self):
        sentence_q = queue.Queue()
        subtitle_q = queue.Queue()
        stop = threading.Event()

        # Source has 어금니 (molar); mocked engine returns the 牙齦 (gum)
        # mistranslation, which the shared target-correction rule rescues.
        with _mock_primary("牙齦很痛"), patch("modules.translator.runtime_events") as events:
            t = translator.start(sentence_q, subtitle_q, stop)
            sentence_q.put({"text": "어금니 아파요", "incomplete": False})
            self.assertEqual(subtitle_q.get(timeout=5), "臼齒很痛")
            stop.set()
            t.join(timeout=2)

        kwargs = events.emit.call_args.kwargs
        self.assertGreaterEqual(kwargs["correction_count"], 1)
        self.assertTrue(
            any(c["before"] == "牙齦" and c["after"] == "臼齒" for c in kwargs["corrections"])
        )

    def test_translation_event_carries_utterance_id(self):
        sentence_q = queue.Queue()
        subtitle_q = queue.Queue()
        stop = threading.Event()

        with _mock_primary("你好"), patch("modules.translator.runtime_events") as events:
            t = translator.start(sentence_q, subtitle_q, stop)
            # utterance_id rides the same sentence-metadata channel as stt_engine,
            # joining this translation event back to its stt/audio events.
            sentence_q.put({"text": "안녕하세요", "incomplete": False, "utterance_id": "utt-42"})
            self.assertEqual(subtitle_q.get(timeout=5), "你好")
            stop.set()
            t.join(timeout=2)

        self.assertEqual(events.emit.call_args.kwargs["utterance_id"], "utt-42")

    def test_translation_event_carries_source_utterance_ids(self):
        sentence_q = queue.Queue()
        subtitle_q = queue.Queue()
        stop = threading.Event()

        with _mock_primary("你好"), patch("modules.translator.runtime_events") as events:
            t = translator.start(sentence_q, subtitle_q, stop)
            # A sentence assembled from several STT chunks lists them all, so a
            # mistranslation can be traced back to each chunk's audio/confidence.
            sentence_q.put({
                "text": "안녕하세요",
                "incomplete": False,
                "utterance_id": "utt-3",
                "source_utterance_ids": ["utt-1", "utt-2", "utt-3"],
                "evidence_source_utterance_ids": ["utt-prior"],
            })
            self.assertEqual(subtitle_q.get(timeout=5), "你好")
            stop.set()
            t.join(timeout=2)

        self.assertEqual(
            events.emit.call_args.kwargs["source_utterance_ids"], ["utt-1", "utt-2", "utt-3"]
        )
        self.assertEqual(events.emit.call_args.kwargs["evidence_source_utterance_ids"], ["utt-prior"])
        self.assertEqual(events.emit.call_args.kwargs["evidence_source_count"], 1)

    def test_translation_event_carries_source_confidence_summary(self):
        sentence_q = queue.Queue()
        subtitle_q = queue.Queue()
        stop = threading.Event()

        with _mock_primary("雿末"), patch("modules.translator.runtime_events") as events:
            t = translator.start(sentence_q, subtitle_q, stop)
            sentence_q.put({
                "text": "안녕하세요",
                "incomplete": False,
                "utterance_id": "utt-3",
                "source_utterance_ids": ["utt-1", "utt-2", "utt-3"],
                "source_avg_logprobs": [-0.3, None, -0.8],
                "source_no_speech_probs": [None, 0.7, 0.2],
                "cut_reason": "merged:forced_blob+natural",
                "forced": True,
                "chunk_count": 3,
                "audio_seconds": 4.5,
            })
            self.assertEqual(subtitle_q.get(timeout=5), "雿末")
            stop.set()
            t.join(timeout=2)

        kwargs = events.emit.call_args.kwargs
        self.assertEqual(kwargs["source_count"], 3)
        self.assertEqual(kwargs["source_avg_logprobs"], [-0.3, None, -0.8])
        self.assertEqual(kwargs["min_avg_logprob"], -0.8)
        self.assertEqual(kwargs["source_no_speech_probs"], [None, 0.7, 0.2])
        self.assertEqual(kwargs["max_no_speech_prob"], 0.7)
        self.assertEqual(kwargs["cut_reason"], "merged:forced_blob+natural")
        self.assertTrue(kwargs["forced"])
        self.assertEqual(kwargs["chunk_count"], 3)
        self.assertEqual(kwargs["audio_seconds"], 4.5)

    def test_translation_event_pads_missing_source_confidence_for_legacy_items(self):
        sentence_q = queue.Queue()
        subtitle_q = queue.Queue()
        stop = threading.Event()

        with _mock_primary("雿末"), patch("modules.translator.runtime_events") as events:
            t = translator.start(sentence_q, subtitle_q, stop)
            sentence_q.put({
                "text": "안녕하세요",
                "incomplete": False,
                "source_utterance_ids": ["utt-1", "utt-2"],
            })
            self.assertEqual(subtitle_q.get(timeout=5), "雿末")
            stop.set()
            t.join(timeout=2)

        kwargs = events.emit.call_args.kwargs
        self.assertEqual(kwargs["source_count"], 2)
        self.assertEqual(kwargs["source_avg_logprobs"], [None, None])
        self.assertIsNone(kwargs["min_avg_logprob"])
        self.assertEqual(kwargs["source_no_speech_probs"], [None, None])
        self.assertIsNone(kwargs["max_no_speech_prob"])

    def test_translation_event_carries_active_profile(self):
        sentence_q = queue.Queue()
        subtitle_q = queue.Queue()
        stop = threading.Event()

        with _active_profile("stellive_hina", use_profile=True), \
                _mock_primary("你好"), patch("modules.translator.runtime_events") as events:
            t = translator.start(sentence_q, subtitle_q, stop)
            sentence_q.put({"text": "안녕하세요", "incomplete": False})
            self.assertEqual(subtitle_q.get(timeout=5), "你好")
            stop.set()
            t.join(timeout=2)

        kwargs = events.emit.call_args.kwargs
        self.assertEqual(kwargs["profile_id"], "stellive_hina")
        self.assertTrue(kwargs["profile_applied"])

    def test_translation_event_marks_profile_not_applied(self):
        # Negative case: a profile id may be configured but use_profile=False means
        # no profile-specific rules were applied — the event must say so.
        sentence_q = queue.Queue()
        subtitle_q = queue.Queue()
        stop = threading.Event()

        with _active_profile("stellive_hina", use_profile=False), \
                _mock_primary("你好"), patch("modules.translator.runtime_events") as events:
            t = translator.start(sentence_q, subtitle_q, stop)
            sentence_q.put({"text": "안녕하세요", "incomplete": False})
            self.assertEqual(subtitle_q.get(timeout=5), "你好")
            stop.set()
            t.join(timeout=2)

        kwargs = events.emit.call_args.kwargs
        self.assertEqual(kwargs["profile_id"], "stellive_hina")
        self.assertFalse(kwargs["profile_applied"])

    def test_translation_event_reports_token_usage(self):
        from modules.translation_engines import _log_token_usage

        sentence_q = queue.Queue()
        subtitle_q = queue.Queue()
        stop = threading.Event()

        def _translate_with_usage(*_a, **_kw):
            _log_token_usage("mock", {"prompt_tokens": 42, "completion_tokens": 7, "total_tokens": 49})
            return "你好"

        with _mock_primary("你好") as engine, patch("modules.translator.runtime_events") as events:
            engine.translate.side_effect = _translate_with_usage
            t = translator.start(sentence_q, subtitle_q, stop)
            sentence_q.put({"text": "안녕하세요", "incomplete": False})
            self.assertEqual(subtitle_q.get(timeout=5), "你好")
            stop.set()
            t.join(timeout=2)

        kwargs = events.emit.call_args.kwargs
        self.assertEqual(kwargs["token_prompt"], 42)
        self.assertEqual(kwargs["token_output"], 7)
        self.assertEqual(kwargs["token_total"], 49)
        # None-valued usage fields (cache_read/cache_write here) must not pollute the event.
        self.assertNotIn("token_cache_read", kwargs)

    def test_translation_event_omits_token_fields_without_usage(self):
        sentence_q = queue.Queue()
        subtitle_q = queue.Queue()
        stop = threading.Event()

        with _mock_primary("你好"), patch("modules.translator.runtime_events") as events:
            t = translator.start(sentence_q, subtitle_q, stop)
            sentence_q.put({"text": "안녕하세요", "incomplete": False})
            self.assertEqual(subtitle_q.get(timeout=5), "你好")
            stop.set()
            t.join(timeout=2)

        kwargs = events.emit.call_args.kwargs
        self.assertNotIn("token_prompt", kwargs)
        self.assertNotIn("token_total", kwargs)

    def test_translator_workers_translate_ahead_but_emit_in_order(self):
        sentence_q = queue.Queue()
        subtitle_q = queue.Queue()
        stop = threading.Event()
        slow_started = threading.Event()
        release_slow = threading.Event()
        fast_finished = threading.Event()

        class _FakeTranslator:
            def __init__(self, shared_state=None):
                pass

            def translate_event(self, text: str, incomplete: bool = False) -> TranslationOutcome:
                if text == "slow":
                    slow_started.set()
                    release_slow.wait(timeout=3)
                if text == "fast":
                    fast_finished.set()
                return TranslationOutcome(
                    source_text=text,
                    target_text=f"zh-{text}",
                    status="success",
                    result_source="api",
                    cache_status="miss",
                    incomplete=incomplete,
                )

        with patch("modules.translator.Translator", _FakeTranslator), \
                patch("modules.translator.runtime_events") as events:
            t = translator.start(sentence_q, subtitle_q, stop)
            sentence_q.put({"text": "slow", "incomplete": False})
            sentence_q.put({"text": "fast", "incomplete": False})

            self.assertTrue(slow_started.wait(timeout=2))
            self.assertTrue(fast_finished.wait(timeout=2))
            self.assertTrue(subtitle_q.empty(), "fast result must not emit before slow result")

            release_slow.set()
            first = subtitle_q.get(timeout=3)
            second = subtitle_q.get(timeout=3)
            stop.set()
            t.join(timeout=2)

        self.assertEqual(first, "zh-slow")
        self.assertEqual(second, "zh-fast")
        emitted = [call.kwargs for call in events.emit.call_args_list]
        self.assertEqual([event["sequence_id"] for event in emitted], [0, 1])
        self.assertEqual([event["source_text"] for event in emitted], ["slow", "fast"])
        self.assertGreater(emitted[1]["predecessor_stall_ms"], 0)
        for event in emitted:
            self.assertGreaterEqual(event["output_delay_ms"], event["engine_latency_ms"])

    def test_translator_runtime_observability_marks_dependency_prefix(self):
        sentence_q = queue.Queue()
        subtitle_q = queue.Queue()
        stop = threading.Event()

        class _FakeTranslator:
            def __init__(self, shared_state=None):
                pass

            def translate_event(self, text: str, incomplete: bool = False) -> TranslationOutcome:
                return TranslationOutcome(
                    source_text=text,
                    target_text=f"zh-{text}",
                    status="success",
                    result_source="api",
                    cache_status="miss",
                    incomplete=incomplete,
                )

        with patch("modules.translator.Translator", _FakeTranslator), \
                patch("modules.translator.runtime_events") as events:
            t = translator.start(sentence_q, subtitle_q, stop)
            sentence_q.put({"text": "그래서 뭐 했어요?", "incomplete": False})
            sentence_q.put({"text": "그게임 좋아요?", "incomplete": False})
            self.assertEqual(subtitle_q.get(timeout=5), "zh-그래서 뭐 했어요?")
            self.assertEqual(subtitle_q.get(timeout=5), "zh-그게임 좋아요?")
            stop.set()
            t.join(timeout=2)

        emitted = [call.kwargs for call in events.emit.call_args_list]
        self.assertEqual(emitted[0]["dependency_marker"], "그래서")
        self.assertTrue(emitted[0]["starts_with_dependency_marker"])
        self.assertEqual(emitted[1]["dependency_marker"], "")
        self.assertFalse(emitted[1]["starts_with_dependency_marker"])

    def test_translator_runtime_observability_reports_queue_wait(self):
        sentence_q = queue.Queue()
        subtitle_q = queue.Queue()
        stop = threading.Event()
        first_started = threading.Event()
        second_started = threading.Event()
        release_workers = threading.Event()

        class _FakeTranslator:
            def __init__(self, shared_state=None):
                pass

            def translate_event(self, text: str, incomplete: bool = False) -> TranslationOutcome:
                if text == "first":
                    first_started.set()
                    release_workers.wait(timeout=3)
                elif text == "second":
                    second_started.set()
                    release_workers.wait(timeout=3)
                return TranslationOutcome(
                    source_text=text,
                    target_text=f"zh-{text}",
                    status="success",
                    result_source="api",
                    cache_status="miss",
                    incomplete=incomplete,
                )

        with patch("modules.translator.Translator", _FakeTranslator), \
                patch("modules.translator.runtime_events") as events:
            t = translator.start(sentence_q, subtitle_q, stop)
            sentence_q.put({"text": "first", "incomplete": False})
            sentence_q.put({"text": "second", "incomplete": False})
            sentence_q.put({"text": "third", "incomplete": False})
            self.assertTrue(first_started.wait(timeout=2))
            self.assertTrue(second_started.wait(timeout=2))
            deadline = time.monotonic() + 2
            while not sentence_q.empty() and time.monotonic() < deadline:
                stop.wait(0.005)
            self.assertTrue(sentence_q.empty())
            release_workers.set()
            self.assertEqual(subtitle_q.get(timeout=3), "zh-first")
            self.assertEqual(subtitle_q.get(timeout=3), "zh-second")
            self.assertEqual(subtitle_q.get(timeout=3), "zh-third")
            stop.set()
            t.join(timeout=2)

        emitted = [call.kwargs for call in events.emit.call_args_list]
        self.assertEqual([event["sequence_id"] for event in emitted], [0, 1, 2])
        for event in emitted:
            self.assertIn("queue_wait_ms", event)
            self.assertGreaterEqual(event["queue_wait_ms"], 0)

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


class TestShutdownOrdering(unittest.TestCase):
    """B1 (#7) — reverse-join shutdown helper.

    Scope is deliberately narrow: we verify the helper does not hang and that
    a stuck thread produces a warning. We do NOT verify that residual queue
    items get processed — that requires the full two-stage shutdown (#7b,
    tracked separately).
    """

    @staticmethod
    def _well_behaved_thread(stop: threading.Event, name: str) -> threading.Thread:
        """A thread that watches stop_event and exits promptly."""
        t = threading.Thread(
            target=lambda: stop.wait(timeout=5),
            name=name,
            daemon=True,
        )
        t.start()
        return t

    @staticmethod
    def _stuck_thread(name: str) -> tuple[threading.Thread, threading.Event]:
        """A thread that ignores stop_event until an explicit release event fires."""
        release = threading.Event()
        t = threading.Thread(
            target=lambda: release.wait(timeout=30),
            name=name,
            daemon=True,
        )
        t.start()
        return t, release

    def test_shutdown_does_not_hang(self):
        from main import _shutdown_threads

        stop = threading.Event()
        threads = [
            self._well_behaved_thread(stop, "Audio"),
            self._well_behaved_thread(stop, "STT"),
            self._well_behaved_thread(stop, "Translator"),
        ]

        started = time.monotonic()
        stuck = _shutdown_threads(threads, stop, join_timeout=2.0)
        elapsed = time.monotonic() - started

        self.assertEqual(stuck, [])
        for t in threads:
            self.assertFalse(t.is_alive(), f"{t.name} still alive after shutdown")
        # All threads watch the same stop_event and exit ~immediately; the whole
        # shutdown should finish well within a single join_timeout window.
        self.assertLess(elapsed, 2.0)

    def test_shutdown_logs_warning_if_thread_stuck(self):
        from main import _shutdown_threads

        stop = threading.Event()
        good = self._well_behaved_thread(stop, "Audio")
        stuck_thread, release = self._stuck_thread("FrozenTranslator")

        try:
            with self.assertLogs(level="WARNING") as cm:
                stuck = _shutdown_threads(
                    [good, stuck_thread], stop, join_timeout=0.2
                )

            self.assertIn(stuck_thread, stuck)
            self.assertNotIn(good, stuck)
            self.assertTrue(
                any("FrozenTranslator" in line and "did not stop" in line for line in cm.output),
                f"Expected stuck-thread warning in logs, got: {cm.output}",
            )
        finally:
            release.set()
            stuck_thread.join(timeout=1)

    def test_shutdown_joins_in_reverse_order(self):
        """Threads are joined in reverse list order (consumer-to-producer)."""
        from main import _shutdown_threads

        join_order: list[str] = []
        stop = threading.Event()

        class _RecordingThread(threading.Thread):
            def join(self, timeout=None):
                join_order.append(self.name)
                super().join(timeout=timeout)

        threads = [
            _RecordingThread(target=lambda: stop.wait(0.5), name="Audio",      daemon=True),
            _RecordingThread(target=lambda: stop.wait(0.5), name="STT",        daemon=True),
            _RecordingThread(target=lambda: stop.wait(0.5), name="Translator", daemon=True),
        ]
        for t in threads:
            t.start()

        _shutdown_threads(threads, stop, join_timeout=1.0)

        self.assertEqual(join_order, ["Translator", "STT", "Audio"])


if __name__ == "__main__":
    unittest.main()
