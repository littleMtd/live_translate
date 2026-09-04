import unittest

from modules.pipeline_events import SegmentInfo, TranscriptionEvent
from modules.profile_context import profile_state
from modules.sentence_buffer import (
    SentenceBuffer,
    _split_prefix_with_reason,
    classify_korean_completeness,
    is_complete,
    split_safe_complete_prefix,
    split_complete_prefix,
)


class TestKoreanCompletenessDecision(unittest.TestCase):
    def test_accepts_narrow_positive_evidence(self):
        expected = {
            "안녕하세요": "strong_final_ending",
            "정말 좋다": "strong_final_ending",
            "끝났어!": "terminal_punctuation",
            '"정말이야?"': "terminal_punctuation",
            "네": "short_acknowledgement",
        }
        for text, reason in expected.items():
            with self.subTest(text=text):
                decision = classify_korean_completeness(text)
                self.assertEqual(decision.state, "complete")
                self.assertEqual(decision.reason, reason)

    def test_vetoes_runtime_particle_adnominal_and_connector_tails(self):
        cases = (
            "선풍기 쓰는 고양이",
            "그냥 편하게 있는",
            "이거 옷 귀엽다 했는데 랑코가",
            "카페에 올렸던 사진 중에",
            "이거 뭐야 이것도 올렸던 것 같은데 앞에",
            "게임 하고",
            "게임이 끝나면",
        )
        for text in cases:
            with self.subTest(text=text):
                decision = classify_korean_completeness(text)
                self.assertEqual(decision.state, "incomplete")
                self.assertIn(
                    decision.reason,
                    {
                        "unfinished_particle",
                        "unfinished_connector",
                        "unfinished_grammatical_tail",
                    },
                )

    def test_terminal_punctuation_does_not_override_grammatical_tail_veto(self):
        for text in (
            "게임 하고.",
            "게임이 끝나면.",
            "이거는.",
            "그냥 편하게 있는.",
            "그때 먹던.",
            "예전에 하던.",
            "내가 알던.",
        ):
            with self.subTest(text=text):
                decision = classify_korean_completeness(text)
                self.assertEqual(decision.state, "incomplete")
                self.assertIn(
                    decision.reason,
                    {
                        "unfinished_particle",
                        "unfinished_connector",
                        "unfinished_adnominal",
                        "unfinished_grammatical_tail",
                    },
                )

    def test_unclosed_delimiter_vetoes_terminal_morphology(self):
        decision = classify_korean_completeness('오늘은 (정말 좋아요')
        self.assertEqual(decision.state, "incomplete")
        self.assertEqual(decision.reason, "unclosed_delimiter")

    def test_decimal_version_url_and_ellipsis_are_not_terminal_boundaries(self):
        for text in ("버전 1.2.", "example.com.", "잠깐...", "어..", "v2.0."):
            with self.subTest(text=text):
                self.assertNotEqual(
                    classify_korean_completeness(text).state,
                    "complete",
                )

    def test_short_misheard_strong_final_is_not_enough_positive_evidence(self):
        decision = classify_korean_completeness("출게요")
        self.assertEqual(decision.state, "uncertain")
        self.assertEqual(decision.reason, "ambiguous_tail")

    def test_safe_prefix_keeps_closing_quote_and_rejects_unsafe_dots(self):
        self.assertEqual(
            split_safe_complete_prefix('"안녕하세요?" 그런데 아직'),
            ('"안녕하세요?"', "그런데 아직"),
        )
        self.assertEqual(
            split_safe_complete_prefix("오늘도 안녕하세요. 그런데 아직"),
            ("오늘도 안녕하세요.", "그런데 아직"),
        )
        for text in ("버전 1.2 입니다", "example.com 주소예요", "잠깐... 아직"):
            with self.subTest(text=text):
                self.assertEqual(split_safe_complete_prefix(text), ("", text))

    def test_safe_prefix_returns_first_boundary_to_bound_subtitle_length(self):
        self.assertEqual(
            split_safe_complete_prefix(
                "첫 번째 문장입니다. 두 번째 문장입니다. 아직 이어지는 내용"
            ),
            ("첫 번째 문장입니다.", "두 번째 문장입니다. 아직 이어지는 내용"),
        )

    def test_safe_prefix_rejects_punctuated_incomplete_grammar(self):
        for text in (
            "오늘은 게임을 하고. 다음 문장입니다",
            "내가 예전에 자주 하던. 다음 문장입니다",
        ):
            with self.subTest(text=text):
                self.assertEqual(split_safe_complete_prefix(text), ("", text))


class TestSentenceBuffer(unittest.TestCase):
    def test_profile_generation_change_requires_explicit_boundary(self):
        first = profile_state.legacy_snapshot("url")
        second = profile_state.legacy_snapshot("isegye_lilpa")
        buffer = SentenceBuffer()
        buffer.push(
            TranscriptionEvent(
                text="first fragment",
                engine="test",
                profile_id="url",
                profile_snapshot=first,
            ),
            now=1.0,
        )
        incoming = TranscriptionEvent(
            text="second fragment",
            engine="test",
            profile_id="isegye_lilpa",
            profile_snapshot=second,
        )
        self.assertTrue(buffer.requires_profile_switch(incoming))
        cut = buffer.flush_profile_switch(2.0)
        self.assertEqual(cut.text, "first fragment")
        self.assertEqual(cut.cut_reason, "profile_switch")
        self.assertIs(cut.source.profile_snapshot, first)

    def test_safe_full_stop_sentence_releases_without_force_cut(self):
        buffer = SentenceBuffer()
        buffer.push(
            TranscriptionEvent(
                text="아, 너무 좋다.",
                engine="elevenlabs",
                profile_id="url",
                utterance_id="utt-live-dot",
            ),
            now=1.0,
        )

        cut = buffer.pop_ready(
            4.0,
            min_wait_seconds=3.0,
            force_cut_seconds=8.0,
        )

        self.assertIsNotNone(cut)
        self.assertEqual(cut.text, "아, 너무 좋다.")
        self.assertEqual(cut.cut_reason, "natural")
        self.assertFalse(cut.forced)
        self.assertFalse(cut.incomplete)

    def test_unsafe_dot_tails_do_not_become_complete(self):
        for text in ("버전 1.2.", "example.com.", "아직...", "v2.0."):
            with self.subTest(text=text):
                self.assertFalse(is_complete(text))

    def test_silence_holds_embedded_question_until_following_predicate(self):
        buffer = SentenceBuffer(silence_complete_enabled=True)
        buffer.push(
            TranscriptionEvent(
                text="그래서 저희가 몇 시부터 몇 시까지를 그 받을지",
                engine="elevenlabs",
                profile_id="url",
                vad_cut_reason="silence",
                utterance_id="utt-8",
                audio_seconds=6.88,
            ),
            now=1.0,
        )

        self.assertIsNone(
            buffer.pop_ready(1.0, min_wait_seconds=5.0, force_cut_seconds=8.0)
        )
        self.assertIsNone(buffer.provisional_snapshot(3.0))

        buffer.push(
            TranscriptionEvent(
                text="그것도 정하면은 되거든요.",
                engine="elevenlabs",
                profile_id="url",
                vad_cut_reason="silence",
                utterance_id="utt-9",
                audio_seconds=3.456,
            ),
            now=2.0,
        )
        cut = buffer.pop_ready(2.0, min_wait_seconds=5.0, force_cut_seconds=8.0)

        self.assertIsNotNone(cut)
        self.assertEqual(
            cut.text,
            "그래서 저희가 몇 시부터 몇 시까지를 그 받을지 그것도 정하면은 되거든요.",
        )
        self.assertEqual(cut.cut_reason, "silence_complete")
        self.assertFalse(cut.incomplete)
        self.assertEqual(cut.source_utterance_ids, ("utt-8", "utt-9"))
        self.assertEqual(cut.chunk_count, 2)
        self.assertEqual(cut.audio_seconds, 10.336)

    def test_other_high_confidence_embedded_question_endings_are_held(self):
        for text in ("누가 오는지", "이게 정답인지"):
            with self.subTest(text=text):
                buffer = SentenceBuffer(silence_complete_enabled=True)
                buffer.push(
                    TranscriptionEvent(
                        text=text,
                        engine="elevenlabs",
                        profile_id="url",
                        vad_cut_reason="silence",
                    ),
                    now=1.0,
                )
                self.assertIsNone(
                    buffer.pop_ready(
                        1.0,
                        min_wait_seconds=5.0,
                        force_cut_seconds=8.0,
                    )
                )

    def test_complete_final_ji_sentence_is_not_blanket_delayed(self):
        buffer = SentenceBuffer(silence_complete_enabled=True)
        buffer.push(
            TranscriptionEvent(
                text="그건 내가 알지.",
                engine="elevenlabs",
                profile_id="url",
                vad_cut_reason="silence",
            ),
            now=1.0,
        )

        cut = buffer.pop_ready(1.0, min_wait_seconds=5.0, force_cut_seconds=8.0)

        self.assertIsNotNone(cut)
        self.assertEqual(cut.text, "그건 내가 알지.")
        self.assertFalse(cut.incomplete)

    def test_punctuated_embedded_question_forms_remain_complete_questions(self):
        for text in ("이게 뭔지?", "알겠는지?", "정답인지?"):
            with self.subTest(text=text):
                self.assertTrue(is_complete(text))

    def test_embedded_question_still_releases_at_force_cut_bound(self):
        buffer = SentenceBuffer(silence_complete_enabled=True)
        buffer.push(
            TranscriptionEvent(
                text="어느 쪽을 받을지",
                engine="elevenlabs",
                profile_id="url",
                vad_cut_reason="silence",
                utterance_id="utt-1",
            ),
            now=1.0,
        )

        cut = buffer.pop_ready(9.0, min_wait_seconds=5.0, force_cut_seconds=8.0)

        self.assertIsNotNone(cut)
        self.assertEqual(cut.text, "어느 쪽을 받을지")
        self.assertEqual(cut.cut_reason, "forced_blob")
        self.assertTrue(cut.forced)
        self.assertTrue(cut.incomplete)

    def test_provisional_snapshot_predicts_same_forced_prefix_without_mutation(self):
        buffer = SentenceBuffer()
        buffer.push(
            TranscriptionEvent(
                text="첫 문장은 끝났어요. 아직 이어지는 중",
                engine="elevenlabs",
                profile_id="url",
                utterance_id="utt-1",
            ),
            now=1.0,
        )

        preview = buffer.provisional_snapshot(3.0)
        final = buffer.pop_ready(
            10.0,
            min_wait_seconds=3.0,
            force_cut_seconds=8.0,
        )

        self.assertIsNotNone(preview)
        self.assertIsNotNone(final)
        self.assertEqual(preview.text, final.text)
        self.assertEqual(preview.incomplete, final.incomplete)
        self.assertEqual(final.cut_reason, "forced_prefix")

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

    def test_silence_cut_can_mark_unpunctuated_buffer_complete(self):
        buffer = SentenceBuffer(silence_complete_enabled=True)
        buffer.push(
            TranscriptionEvent(
                text="지금 여기까지 말했어",
                engine="groq",
                profile_id="a",
                vad_cut_reason="silence",
            ),
            now=1.0,
        )

        cut = buffer.pop_ready(1.0, min_wait_seconds=5.0, force_cut_seconds=8.0)

        self.assertIsNotNone(cut)
        self.assertEqual(cut.text, "지금 여기까지 말했어")
        self.assertEqual(cut.cut_reason, "silence_complete")
        self.assertFalse(cut.incomplete)
        self.assertFalse(cut.forced)

    def test_silence_cut_does_not_complete_incomplete_ending(self):
        buffer = SentenceBuffer(silence_complete_enabled=True)
        buffer.push(
            TranscriptionEvent(
                text="지금 게임 하고",
                engine="groq",
                profile_id="a",
                vad_cut_reason="silence",
            ),
            now=1.0,
        )

        self.assertIsNone(buffer.pop_ready(1.0, min_wait_seconds=5.0, force_cut_seconds=8.0))

        cut = buffer.pop_ready(11.0, min_wait_seconds=5.0, force_cut_seconds=8.0)
        self.assertIsNotNone(cut)
        self.assertEqual(cut.cut_reason, "forced_blob")
        self.assertTrue(cut.forced)

    def test_silence_cut_does_not_complete_too_short_text(self):
        buffer = SentenceBuffer(silence_complete_enabled=True)
        buffer.push(
            TranscriptionEvent(
                text="네",
                engine="groq",
                profile_id="a",
                vad_cut_reason="silence",
            ),
            now=1.0,
        )

        self.assertIsNone(buffer.pop_ready(1.0, min_wait_seconds=5.0, force_cut_seconds=8.0))

        cut = buffer.pop_ready(11.0, min_wait_seconds=5.0, force_cut_seconds=8.0)
        self.assertIsNotNone(cut)
        self.assertEqual(cut.cut_reason, "forced_blob")
        self.assertTrue(cut.forced)

    def test_segment_gap_can_supply_forced_prefix_boundary(self):
        buffer = SentenceBuffer(segment_gap_split_enabled=True, segment_gap_seconds=0.6)
        buffer.push(
            TranscriptionEvent(
                text="첫번째 긴 문장 두번째 이어지는 말",
                engine="groq",
                profile_id="a",
                segments=(
                    SegmentInfo(start=0.0, end=1.0, text="첫번째 긴 문장"),
                    SegmentInfo(start=1.8, end=2.5, text="두번째 이어지는 말"),
                ),
            ),
            now=1.0,
        )

        cut = buffer.pop_ready(11.0, min_wait_seconds=5.0, force_cut_seconds=8.0)

        self.assertIsNotNone(cut)
        self.assertEqual(cut.text, "첫번째 긴 문장")
        self.assertEqual(cut.cut_reason, "forced_gap_prefix")
        self.assertFalse(cut.incomplete)
        self.assertTrue(cut.forced)

    def test_segment_gap_boundary_requires_space_at_boundary(self):
        self.assertEqual(
            _split_prefix_with_reason("abcdef ghij", (3, 6)),
            ("abcdef", "ghij", "forced_gap_prefix", 7),
        )

    def test_segment_gap_boundary_accounts_for_all_stripped_whitespace(self):
        self.assertEqual(
            _split_prefix_with_reason("abcdef   ghij", (3, 6)),
            ("abcdef", "ghij", "forced_gap_prefix", 9),
        )

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

    def test_forced_prefix_residual_moves_prior_chunks_to_evidence(self):
        # Carried residual chunks remain available as evidence, but they do not
        # count as current source/audio for the residual-derived sentence.
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
        self.assertEqual(cut.evidence_source_utterance_ids, ())

        # Residual carried back keeps utt-1 as evidence. Only the new chunk is
        # current-source/current-audio for the next sentence.
        buffer.push(
            TranscriptionEvent(
                text="계속합니다", engine="groq", profile_id="a",
                utterance_id="utt-2", audio_seconds=1.0,
            ),
            now=14.0,
        )
        cut2 = buffer.pop_ready(20.0, min_wait_seconds=5.0, force_cut_seconds=8.0)
        self.assertNotIn("utt-1", cut2.source_utterance_ids)
        self.assertEqual(cut2.source_utterance_ids, ("utt-2",))
        self.assertEqual(cut2.evidence_source_utterance_ids, ("utt-1",))
        self.assertEqual(cut2.chunk_count, 1)
        self.assertEqual(cut2.audio_seconds, 1.0)

    def test_forced_prefix_residual_does_not_double_count_confidence_arrays(self):
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
        self.assertEqual(cut.evidence_source_utterance_ids, ())

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

        self.assertEqual(cut2.source_utterance_ids, ("utt-2",))
        self.assertEqual(cut2.evidence_source_utterance_ids, ("utt-1",))
        self.assertEqual(cut2.source_avg_logprobs, (None,))
        self.assertEqual(cut2.source_no_speech_probs, (0.9,))
        self.assertEqual(cut2.chunk_count, 1)

    def test_pure_residual_forced_blob_can_have_empty_current_source(self):
        buffer = SentenceBuffer()
        buffer.push(
            TranscriptionEvent(
                text="complete prefix! residual only",
                engine="groq",
                profile_id="a",
                utterance_id="utt-1",
                audio_seconds=3.0,
            ),
            now=1.0,
        )

        cut = buffer.pop_ready(11.0, min_wait_seconds=5.0, force_cut_seconds=8.0)
        self.assertEqual(cut.cut_reason, "forced_prefix")

        residual = buffer.pop_ready(20.0, min_wait_seconds=5.0, force_cut_seconds=8.0)

        self.assertEqual(residual.cut_reason, "forced_blob")
        self.assertEqual(residual.source_utterance_ids, ())
        self.assertEqual(residual.evidence_source_utterance_ids, ("utt-1",))
        self.assertEqual(residual.chunk_count, 0)
        self.assertEqual(residual.audio_seconds, 0.0)

    def test_natural_cut_after_carry_keeps_evidence_out_of_current_source(self):
        buffer = SentenceBuffer()
        buffer.push(
            TranscriptionEvent(
                text="complete prefix! residual",
                engine="groq",
                profile_id="a",
                utterance_id="utt-1",
                audio_seconds=3.0,
            ),
            now=1.0,
        )

        cut = buffer.pop_ready(11.0, min_wait_seconds=5.0, force_cut_seconds=8.0)
        self.assertEqual(cut.cut_reason, "forced_prefix")

        buffer.push(
            TranscriptionEvent(
                text="done!",
                engine="groq",
                profile_id="a",
                utterance_id="utt-2",
                audio_seconds=1.0,
            ),
            now=11.1,
        )
        natural = buffer.pop_ready(11.5, min_wait_seconds=0.3, force_cut_seconds=8.0)

        self.assertEqual(natural.cut_reason, "natural")
        self.assertEqual(natural.source_utterance_ids, ("utt-2",))
        self.assertEqual(natural.evidence_source_utterance_ids, ("utt-1",))
        self.assertEqual(natural.chunk_count, 1)
        self.assertEqual(natural.audio_seconds, 1.0)

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
