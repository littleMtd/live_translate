import unittest

from modules.pipeline_events import TranscriptionEvent
from modules.sentence_buffer import (
    SentenceBuffer,
    is_complete,
    split_complete_prefix,
)


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

    def test_natural_cut_reports_assembly_metadata(self):
        buffer = SentenceBuffer()
        buffer.push(
            TranscriptionEvent(text="안녕", engine="groq", profile_id="a", audio_seconds=0.8),
            now=1.0,
        )
        buffer.push(
            TranscriptionEvent(text="하세요", engine="groq", profile_id="a", audio_seconds=0.5),
            now=1.1,
        )

        cut = buffer.pop_ready(1.5, min_wait_seconds=0.3, force_cut_seconds=2.0)

        self.assertIsNotNone(cut)
        self.assertEqual(cut.cut_reason, "natural")
        self.assertEqual(cut.chunk_count, 2)
        self.assertEqual(cut.audio_seconds, 1.3)

    def test_natural_cut_collects_all_source_utterance_ids(self):
        buffer = SentenceBuffer()
        buffer.push(
            TranscriptionEvent(text="안녕", engine="groq", profile_id="a", utterance_id="utt-1"),
            now=1.0,
        )
        buffer.push(
            TranscriptionEvent(text="하세요", engine="groq", profile_id="a", utterance_id="utt-2"),
            now=1.1,
        )

        cut = buffer.pop_ready(1.5, min_wait_seconds=0.3, force_cut_seconds=2.0)

        self.assertEqual(cut.source_utterance_ids, ("utt-1", "utt-2"))

    def test_natural_cut_collects_aligned_source_confidence_arrays(self):
        buffer = SentenceBuffer()
        buffer.push(
            TranscriptionEvent(
                text="first",
                engine="groq",
                profile_id="a",
                utterance_id="utt-1",
                avg_logprob=-0.5,
                no_speech_prob=None,
            ),
            now=1.0,
        )
        buffer.push(
            TranscriptionEvent(
                text="done!",
                engine="groq",
                profile_id="a",
                utterance_id="utt-2",
                avg_logprob=None,
                no_speech_prob=0.8,
            ),
            now=1.1,
        )

        cut = buffer.pop_ready(1.5, min_wait_seconds=0.3, force_cut_seconds=2.0)

        self.assertEqual(cut.source_utterance_ids, ("utt-1", "utt-2"))
        self.assertEqual(cut.source_avg_logprobs, (-0.5, None))
        self.assertEqual(cut.source_no_speech_probs, (None, 0.8))

    def test_forced_blob_cut_reason_and_counts(self):
        buffer = SentenceBuffer()
        buffer.push(
            TranscriptionEvent(text="지금 게임 하고", engine="groq", profile_id="a", audio_seconds=2.0),
            now=1.0,
        )

        cut = buffer.pop_ready(2.0, min_wait_seconds=5.0, force_cut_seconds=0.5)

        self.assertEqual(cut.cut_reason, "forced_blob")
        self.assertTrue(cut.incomplete)
        self.assertEqual(cut.chunk_count, 1)
        self.assertEqual(cut.audio_seconds, 2.0)

    def test_forced_blob_keeps_source_confidence_arrays(self):
        buffer = SentenceBuffer()
        buffer.push(
            TranscriptionEvent(
                text="first fragment",
                engine="groq",
                profile_id="a",
                utterance_id="utt-1",
                avg_logprob=-0.9,
                no_speech_prob=0.1,
            ),
            now=1.0,
        )
        buffer.push(
            TranscriptionEvent(
                text="second fragment",
                engine="groq",
                profile_id="a",
                utterance_id="utt-2",
                avg_logprob=-0.2,
                no_speech_prob=None,
            ),
            now=1.1,
        )

        cut = buffer.pop_ready(2.0, min_wait_seconds=5.0, force_cut_seconds=0.5)

        self.assertEqual(cut.cut_reason, "forced_blob")
        self.assertEqual(cut.source_utterance_ids, ("utt-1", "utt-2"))
        self.assertEqual(cut.source_avg_logprobs, (-0.9, -0.2))
        self.assertEqual(cut.source_no_speech_probs, (0.1, None))

    def test_forced_prefix_residual_keeps_source_attribution(self):
        # A chunk can straddle the punctuation boundary, so the residual must
        # NOT drop the source ids/audio of the chunks seen so far — otherwise a
        # mistranslation in the carried-over text becomes un-attributable.
        buffer = SentenceBuffer()
        buffer.push(
            TranscriptionEvent(
                text="오늘 날씨 진짜 좋네요. 그래서 산책 길게 갈까",
                engine="groq",
                profile_id="a",
                utterance_id="utt-1",
                audio_seconds=3.0,
            ),
            now=1.0,
        )

        cut = buffer.pop_ready(11.0, min_wait_seconds=5.0, force_cut_seconds=8.0)

        self.assertEqual(cut.cut_reason, "forced_prefix")
        self.assertEqual(cut.source_utterance_ids, ("utt-1",))

        # Residual carried back keeps utt-1; the next sentence still lists it
        # (plus the new chunk) so the earlier audio remains attributable.
        buffer.push(
            TranscriptionEvent(
                text="계속합니다", engine="groq", profile_id="a",
                utterance_id="utt-2", audio_seconds=1.0,
            ),
            now=14.0,
        )
        cut2 = buffer.pop_ready(20.0, min_wait_seconds=5.0, force_cut_seconds=8.0)
        self.assertIn("utt-1", cut2.source_utterance_ids)
        self.assertIn("utt-2", cut2.source_utterance_ids)
        self.assertEqual(cut2.chunk_count, 2)
        self.assertEqual(cut2.audio_seconds, 4.0)

    def test_forced_prefix_residual_keeps_source_confidence_arrays(self):
        buffer = SentenceBuffer()
        buffer.push(
            TranscriptionEvent(
                text="complete prefix! residual still going",
                engine="groq",
                profile_id="a",
                utterance_id="utt-1",
                avg_logprob=-0.6,
                no_speech_prob=0.3,
            ),
            now=1.0,
        )

        cut = buffer.pop_ready(11.0, min_wait_seconds=5.0, force_cut_seconds=8.0)

        self.assertEqual(cut.cut_reason, "forced_prefix")
        self.assertEqual(cut.source_avg_logprobs, (-0.6,))
        self.assertEqual(cut.source_no_speech_probs, (0.3,))

        buffer.push(
            TranscriptionEvent(
                text="done!",
                engine="groq",
                profile_id="a",
                utterance_id="utt-2",
                avg_logprob=None,
                no_speech_prob=0.9,
            ),
            now=14.0,
        )
        cut2 = buffer.pop_ready(20.0, min_wait_seconds=5.0, force_cut_seconds=8.0)

        self.assertEqual(cut2.source_utterance_ids, ("utt-1", "utt-2"))
        self.assertEqual(cut2.source_avg_logprobs, (-0.6, None))
        self.assertEqual(cut2.source_no_speech_probs, (0.3, 0.9))

    def test_reset_clears_pending_text(self):
        buffer = SentenceBuffer()
        buffer.push("안녕하세요", now=1.0)
        buffer.reset()

        self.assertIsNone(buffer.pop_ready(2.0, min_wait_seconds=0.0, force_cut_seconds=0.0))

    def test_is_complete_delegates_sentence_ending_rules(self):
        self.assertTrue(is_complete("안녕하세요"))
        self.assertFalse(is_complete("지금 게임 하고"))


class TestSplitCompletePrefix(unittest.TestCase):
    """Task #10 B1 v1 — punctuation-only internal split."""

    def test_punctuation_boundary_split(self):
        self.assertEqual(
            split_complete_prefix("안녕하세요. 그래서"),
            ("안녕하세요.", "그래서"),
        )

    def test_takes_last_safe_punctuation_boundary(self):
        # Multi-sentence blob → split at the LAST boundary, not the first.
        self.assertEqual(
            split_complete_prefix("안녕. 좋아요? 그런데 말이야"),
            ("안녕. 좋아요?", "그런데 말이야"),
        )

    def test_no_boundary_returns_empty_prefix(self):
        self.assertEqual(
            split_complete_prefix("지금 게임 하고 있는데"),
            ("", "지금 게임 하고 있는데"),
        )

    def test_trailing_boundary_gives_empty_residual(self):
        # Leading `다`(=all) must NOT be split — only the final '.' is a
        # boundary, proving punctuation-only avoids the 다 false-split risk.
        self.assertEqual(
            split_complete_prefix("다 끝났습니다."),
            ("다 끝났습니다.", ""),
        )

    def test_bare_morpheme_is_not_a_boundary(self):
        # No punctuation anywhere → not splittable, even though it contains
        # plenty of 다/요 syllables mid-utterance.
        self.assertEqual(
            split_complete_prefix("거의 다 하고 다들 좋다고 하니까"),
            ("", "거의 다 하고 다들 좋다고 하니까"),
        )


class TestForcedSplitBehavior(unittest.TestCase):
    def test_recoverable_emits_complete_prefix_and_carries_residual(self):
        buffer = SentenceBuffer()
        buffer.push("오늘 날씨 진짜 좋네요. 그래서 산책 길게 갈까", now=1.0)

        cut = buffer.pop_ready(11.0, min_wait_seconds=5.0, force_cut_seconds=8.0)

        self.assertIsNotNone(cut)
        self.assertEqual(cut.text, "오늘 날씨 진짜 좋네요.")
        self.assertFalse(cut.incomplete)
        self.assertTrue(cut.forced)

        # Residual (>3 sig chars) carried back; clock reset to `now` so it does
        # NOT immediately force-cut on the next pop.
        self.assertIsNone(
            buffer.pop_ready(13.0, min_wait_seconds=5.0, force_cut_seconds=8.0)
        )
        buffer.push("이야기를 계속합니다", now=14.0)
        cut2 = buffer.pop_ready(20.0, min_wait_seconds=5.0, force_cut_seconds=8.0)
        self.assertIsNotNone(cut2)
        self.assertEqual(cut2.text, "그래서 산책 길게 갈까 이야기를 계속합니다")
        self.assertTrue(cut2.forced)

    def test_trivial_residual_is_dropped(self):
        buffer = SentenceBuffer()
        # No trailing period after `응`, so the last boundary is the '.' after
        # `재밌었어요`; `응` becomes a trivial (<=3 sig) residual.
        buffer.push("오늘 진짜 재밌었어요. 응", now=1.0)

        cut = buffer.pop_ready(11.0, min_wait_seconds=5.0, force_cut_seconds=8.0)

        self.assertEqual(cut.text, "오늘 진짜 재밌었어요.")
        self.assertFalse(cut.incomplete)
        self.assertTrue(cut.forced)
        # `응` (<=3 sig) dropped → buffer fully reset, nothing pending.
        self.assertIsNone(
            buffer.pop_ready(30.0, min_wait_seconds=5.0, force_cut_seconds=8.0)
        )

    def test_short_prefix_keeps_whole_blob_forced(self):
        buffer = SentenceBuffer()
        # prefix "네." has <6 significant chars → do NOT split, original
        # whole-blob forced behavior preserved.
        buffer.push("네. 그래서 그것은 길게 이어지는 말입니다만", now=1.0)

        cut = buffer.pop_ready(11.0, min_wait_seconds=5.0, force_cut_seconds=8.0)

        self.assertEqual(cut.text, "네. 그래서 그것은 길게 이어지는 말입니다만")
        self.assertTrue(cut.incomplete)
        self.assertTrue(cut.forced)

    def test_genuine_partial_without_boundary_stays_incomplete(self):
        buffer = SentenceBuffer()
        buffer.push("지금 게임 하고", now=1.0)

        cut = buffer.pop_ready(11.0, min_wait_seconds=5.0, force_cut_seconds=8.0)

        self.assertEqual(cut.text, "지금 게임 하고")
        self.assertTrue(cut.incomplete)
        self.assertTrue(cut.forced)

    def test_min_wait_clean_cut_does_not_split(self):
        buffer = SentenceBuffer()
        # Whole utterance is complete (ends 요). Clean-cut path must emit it
        # WHOLE — split_complete_prefix is forced-path only.
        buffer.push("좋아. 정말 재밌어요", now=1.0)

        cut = buffer.pop_ready(1.5, min_wait_seconds=0.3, force_cut_seconds=8.0)

        self.assertIsNotNone(cut)
        self.assertEqual(cut.text, "좋아. 정말 재밌어요")
        self.assertFalse(cut.incomplete)
        self.assertFalse(cut.forced)


if __name__ == "__main__":
    unittest.main()
