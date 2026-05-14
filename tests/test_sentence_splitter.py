import queue
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from modules.sentence_splitter import _is_complete, start
from modules.pipeline_events import TranscriptionEvent

# Fast config used by all thread tests: min_wait=0.3s, force_cut=0.8s.
# Default config (3s / 8s) would make thread tests take 10–30 s each.
def _fast_cfg(min_wait: float = 0.3, force_cut: float = 0.8) -> MagicMock:
    m = MagicMock()
    m.splitter.min_wait_seconds = min_wait
    m.splitter.force_cut_seconds = force_cut
    return m


class TestIsComplete(unittest.TestCase):

    def test_complete_endings(self):
        cases = [
            "안녕하세요",       # 요
            "그렇죠",           # 죠
            "맞다",             # 다
            "진짜야",           # 야
            "ㅋㅋ",             # ㅋㅋ
            "정말요?",          # ?
            "대박!",            # !
            "괜찮아",           # 아
            "그렇네",           # 네
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertTrue(_is_complete(text), f"Expected complete: {text!r}")

    def test_incomplete_endings(self):
        cases = [
            "지금 게임 하고",   # 고
            "밥을 먹으면",       # 면
            "그래서",           # 서
            "왜냐하면 니까",     # 니까
            "알잖아 거든",       # 거든
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertFalse(_is_complete(text), f"Expected incomplete: {text!r}")

    def test_unknown_ending_returns_false(self):
        # 未知語尾預設為 False（不切）
        self.assertFalse(_is_complete("뭔가이상한"))

    def test_empty_string(self):
        self.assertFalse(_is_complete(""))

    def test_whitespace_only(self):
        self.assertFalse(_is_complete("   "))

    def test_new_complete_endings(self):
        cases = [
            "그렇군",       # 군
            "그렇구나",     # 구나
            "가겠어",       # 겠어
            "먹겠다",       # 겠다
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertTrue(_is_complete(text), f"Expected complete: {text!r}")

    def test_new_incomplete_endings(self):
        cases = [
            "학교에서",     # 에서
            "집으로",       # 으로
            "밥을 먹아서",  # 아서
            "공부하고",     # 하고
            "여기이고",     # 이고
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertFalse(_is_complete(text), f"Expected incomplete: {text!r}")

    def test_incomplete_overrides_complete_substring(self):
        # "배고서" ends with 서 (incomplete 이서/어서 family) even though 고 is also checked
        self.assertFalse(_is_complete("배고서"))


class TestSentenceSplitterThread(unittest.TestCase):

    def _run(self, tokens: list[str], wait: float) -> list[dict]:
        tq: queue.Queue = queue.Queue()
        sq: queue.Queue = queue.Queue()
        stop = threading.Event()
        with patch("modules.sentence_splitter.cfg", _fast_cfg()):
            start(tq, sq, stop)
            for token in tokens:
                tq.put(token)
            time.sleep(wait)
            stop.set()

        results = []
        while not sq.empty():
            results.append(sq.get_nowait())
        return results

    def test_complete_sentence_sent(self):
        results = self._run(["안녕하세요"], wait=1.0)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["incomplete"], False)

    def test_force_cut_marks_incomplete(self):
        # 語尾是 "고"（incomplete），等待超過 force_cut=0.8s
        results = self._run(["게임 하고"], wait=1.5)
        self.assertGreater(len(results), 0)
        self.assertTrue(results[0]["incomplete"])

    def test_buffer_accumulates_multiple_tokens(self):
        results = self._run(["진짜", "대박이에요"], wait=1.0)
        self.assertEqual(len(results), 1)
        self.assertIn("진짜", results[0]["text"])
        self.assertIn("대박이에요", results[0]["text"])

    def test_empty_queue_no_output(self):
        results = self._run([], wait=0.5)
        self.assertEqual(len(results), 0)

    def test_transcription_event_metadata_is_propagated(self):
        token = TranscriptionEvent(
            text="안녕하세요",
            engine="groq",
            profile_id="isegye_lilpa",
            avg_logprob=-0.2,
            no_speech_prob=0.1,
        )
        results = self._run([token], wait=1.0)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["text"], token.text)
        self.assertEqual(results[0]["stt_engine"], "groq")
        self.assertEqual(results[0]["profile_id"], "isegye_lilpa")
        self.assertEqual(results[0]["avg_logprob"], -0.2)
        self.assertEqual(results[0]["no_speech_prob"], 0.1)


class TestSentenceSplitterPause(unittest.TestCase):

    def test_pause_prevents_output(self):
        tq: queue.Queue = queue.Queue()
        sq: queue.Queue = queue.Queue()
        stop = threading.Event()
        pause = threading.Event()
        pause.set()   # start paused

        with patch("modules.sentence_splitter.cfg", _fast_cfg()):
            start(tq, sq, stop, pause_event=pause)
            tq.put("안녕하세요")   # complete ending — would normally emit
            time.sleep(0.5)
            stop.set()

        self.assertTrue(sq.empty(), "No output expected while paused")

    def test_resume_after_pause_produces_output(self):
        tq: queue.Queue = queue.Queue()
        sq: queue.Queue = queue.Queue()
        stop = threading.Event()
        pause = threading.Event()

        with patch("modules.sentence_splitter.cfg", _fast_cfg()):
            start(tq, sq, stop, pause_event=pause)
            # Add a token, immediately pause, wait, then unpause
            tq.put("환영합니다")   # 다 = complete
            time.sleep(0.1)
            pause.set()
            time.sleep(0.3)
            pause.clear()
            # Wait > 0.5 s so the splitter's pause-loop tick expires and the
            # drain completes before we add the post-resume token.
            time.sleep(0.7)
            tq.put("감사합니다")   # 다 = complete, arrives after drain
            time.sleep(1.0)
            stop.set()

        results = []
        while not sq.empty():
            results.append(sq.get_nowait())
        # At least the post-resume sentence should arrive
        self.assertGreater(len(results), 0)
        texts = " ".join(r["text"] for r in results)
        self.assertIn("감사합니다", texts)


if __name__ == "__main__":
    unittest.main()
