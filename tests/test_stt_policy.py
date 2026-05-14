import logging
import unittest

from modules.stt_policy import (
    build_groq_prompt,
    is_hallucinated,
    normalize_prompt_text,
    segment_stats,
    should_reject_language,
    should_reject_segments,
)


class TestSttPolicy(unittest.TestCase):
    def test_normalize_prompt_text_collapses_whitespace_and_truncates(self):
        self.assertEqual(normalize_prompt_text("  one   two  three  ", max_chars=7), "o three")

    def test_is_hallucinated_rejects_empty_text(self):
        self.assertTrue(is_hallucinated("   ", max_japanese_chars=2))

    def test_is_hallucinated_rejects_japanese_kana_over_threshold(self):
        self.assertTrue(is_hallucinated("こんにちは", max_japanese_chars=2))

    def test_is_hallucinated_rejects_repetition_loop(self):
        self.assertTrue(is_hallucinated("one two three one two three", max_japanese_chars=2))

    def test_is_hallucinated_allows_normal_korean(self):
        self.assertFalse(is_hallucinated("안녕하세요 반갑습니다", max_japanese_chars=2))

    def test_should_reject_language_rejects_japanese(self):
        self.assertTrue(should_reject_language("ja", "こんにちは", logging.getLogger("test")))

    def test_should_reject_language_allows_unexpected_non_japanese(self):
        self.assertFalse(should_reject_language("en", "hello", logging.getLogger("test")))

    def test_segment_stats_averages_segment_metadata(self):
        stats = segment_stats([
            {"no_speech_prob": 0.2, "avg_logprob": -0.4, "compression_ratio": 1.0},
            {"no_speech_prob": 0.4, "avg_logprob": -0.6, "compression_ratio": 2.0},
        ])

        self.assertIsNotNone(stats)
        self.assertAlmostEqual(stats.no_speech, 0.3)
        self.assertAlmostEqual(stats.logprob, -0.5)
        self.assertAlmostEqual(stats.compression_ratio, 1.5)

    def test_should_reject_segments_uses_thresholds(self):
        self.assertTrue(
            should_reject_segments(
                [{"no_speech_prob": 0.9, "avg_logprob": -0.1, "compression_ratio": 1.0}],
                text="noise",
                no_speech_threshold=0.6,
                avg_logprob_threshold=-1.0,
                logger=logging.getLogger("test"),
            )
        )

    def test_build_groq_prompt_combines_seed_glossary_and_recent_context(self):
        prompt = build_groq_prompt(
            seed_prompt=" seed  prompt ",
            use_profile_glossary=True,
            active_profile="profile",
            last_transcript=" recent   transcript ",
            glossary_builder=lambda profile: f"{profile} glossary",
            max_context_chars=50,
        )

        self.assertEqual(
            prompt,
            "seed prompt\nprofile glossary\nRecent Korean transcript context: recent transcript",
        )


if __name__ == "__main__":
    unittest.main()
