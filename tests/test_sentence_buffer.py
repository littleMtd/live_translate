import unittest

from modules.pipeline_events import TranscriptionEvent
from modules.sentence_buffer import SentenceBuffer, is_complete


class TestSentenceBuffer(unittest.TestCase):
    def test_push_accumulates_tokens_until_complete_cut(self):
        buffer = SentenceBuffer()
        buffer.push("안녕", now=10.0)
        buffer.push("하세요", now=10.1)

        cut = buffer.pop_ready(
            10.5,
            min_wait_seconds=0.3,
            force_cut_seconds=2.0,
        )

        self.assertIsNotNone(cut)
        self.assertEqual(cut.text, "안녕 하세요")
        self.assertFalse(cut.incomplete)
        self.assertFalse(cut.forced)

    def test_force_cut_marks_incomplete_when_ending_is_not_complete(self):
        buffer = SentenceBuffer()
        buffer.push("지금 게임 하고", now=1.0)

        cut = buffer.pop_ready(
            2.0,
            min_wait_seconds=5.0,
            force_cut_seconds=0.5,
        )

        self.assertIsNotNone(cut)
        self.assertEqual(cut.text, "지금 게임 하고")
        self.assertTrue(cut.incomplete)
        self.assertTrue(cut.forced)

    def test_metadata_tracks_latest_transcription_event(self):
        first = TranscriptionEvent(text="안녕", engine="sensevoice", profile_id="a")
        latest = TranscriptionEvent(
            text="하세요",
            engine="groq",
            profile_id="b",
            avg_logprob=-0.1,
            no_speech_prob=0.2,
        )
        buffer = SentenceBuffer()
        buffer.push(first, now=1.0)
        buffer.push(latest, now=1.1)

        cut = buffer.pop_ready(1.5, min_wait_seconds=0.3, force_cut_seconds=2.0)

        self.assertIsNotNone(cut)
        self.assertIs(cut.source, latest)

    def test_reset_clears_pending_text(self):
        buffer = SentenceBuffer()
        buffer.push("안녕하세요", now=1.0)
        buffer.reset()

        self.assertIsNone(buffer.pop_ready(2.0, min_wait_seconds=0.0, force_cut_seconds=0.0))

    def test_is_complete_delegates_sentence_ending_rules(self):
        self.assertTrue(is_complete("안녕하세요"))
        self.assertFalse(is_complete("지금 게임 하고"))


if __name__ == "__main__":
    unittest.main()
