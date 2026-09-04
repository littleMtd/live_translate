import logging
import unittest

from modules.stt_policy import (
    build_groq_prompt,
    build_groq_prompt_budget,
    dedupe_transcript_overlap,
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

    def test_is_hallucinated_allows_explicitly_detected_japanese(self):
        self.assertFalse(
            is_hallucinated("今日は楽しかったです", max_japanese_chars=2, allow_japanese=True)
        )

    def test_japanese_allowance_does_not_disable_repetition_filter(self):
        self.assertTrue(
            is_hallucinated(
                "今日は とても 楽しい 今日は とても 楽しい",
                max_japanese_chars=2,
                allow_japanese=True,
            )
        )

    def test_is_hallucinated_rejects_repetition_loop(self):
        self.assertTrue(is_hallucinated("one two three one two three", max_japanese_chars=2))

    def test_is_hallucinated_honors_repeat_ratio_setting(self):
        text = "one two three one two three tail tail"
        self.assertTrue(is_hallucinated(text, max_japanese_chars=2, max_repeat_ratio=0.7))
        self.assertFalse(is_hallucinated(text, max_japanese_chars=2, max_repeat_ratio=0.8))

    def test_is_hallucinated_allows_short_repeated_emphasis(self):
        text = "okay okay okay okay"
        self.assertFalse(is_hallucinated(text, max_japanese_chars=2, max_repeat_ratio=0.7))

    def test_is_hallucinated_allows_normal_korean(self):
        self.assertFalse(is_hallucinated("안녕하세요 반갑습니다", max_japanese_chars=2))

    def test_should_reject_language_rejects_japanese(self):
        self.assertTrue(should_reject_language("ja", "こんにちは", logging.getLogger("test")))

    def test_should_reject_language_allows_japanese_when_policy_enabled(self):
        self.assertFalse(
            should_reject_language(
                "ja", "こんにちは", logging.getLogger("test"), allow_japanese=True
            )
        )

    def test_should_reject_language_allows_unexpected_non_japanese(self):
        self.assertFalse(should_reject_language("en", "hello", logging.getLogger("test")))

    def test_warning_logs_do_not_include_rejected_speech(self):
        sentinel = "PRIVATE-SPOKEN-CONTENT"
        logger = logging.getLogger("test.stt.redaction")

        with self.assertLogs(logger, level="WARNING") as captured:
            self.assertTrue(should_reject_language("ja", sentinel, logger))
            self.assertTrue(
                should_reject_segments(
                    [{"no_speech_prob": 0.9, "avg_logprob": 0.0, "compression_ratio": 1.0}],
                    text=sentinel,
                    no_speech_threshold=0.6,
                    avg_logprob_threshold=-1.0,
                    logger=logger,
                )
            )

        self.assertNotIn(sentinel, "\n".join(captured.output))

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

    def test_build_groq_prompt_respects_max_prompt_chars(self):
        prompt = build_groq_prompt(
            seed_prompt="seed prompt",
            use_profile_glossary=True,
            active_profile="profile",
            last_transcript="recent context " * 20,
            glossary_builder=lambda profile: "term " * 100,
            max_context_chars=120,
            max_prompt_chars=80,
        )

        self.assertIsNotNone(prompt)
        self.assertLessEqual(len(prompt.encode("utf-8")), 80)
        self.assertTrue(prompt.startswith("seed prompt"))
        self.assertIn("Recent Korean transcript context:", prompt)

    def test_build_groq_prompt_respects_max_prompt_chars_for_korean_text(self):
        prompt = build_groq_prompt(
            seed_prompt="한국어 방송",
            use_profile_glossary=True,
            active_profile="profile",
            last_transcript="최근 문맥 " * 20,
            glossary_builder=lambda profile: "용어 " * 100,
            max_context_chars=120,
            max_prompt_chars=80,
        )

        self.assertIsNotNone(prompt)
        self.assertLessEqual(len(prompt.encode("utf-8")), 80)
        self.assertIn("Recent Korean transcript context:", prompt)

    def test_prompt_budget_clean_fit_reports_everything_present(self):
        budget = build_groq_prompt_budget(
            seed_prompt="seed prompt",
            use_profile_glossary=True,
            active_profile="profile",
            last_transcript="recent transcript",
            glossary_builder=lambda profile: f"{profile} glossary",
            max_context_chars=50,
        )

        self.assertTrue(budget.glossary_present)
        self.assertFalse(budget.glossary_truncated)
        self.assertTrue(budget.context_present)
        self.assertTrue(budget.context_included)
        self.assertEqual(budget.prompt_bytes, len(budget.prompt.encode("utf-8")))

    def test_prompt_budget_tight_limit_sacrifices_glossary_keeps_context(self):
        # The exact regression we want visibility on: a long glossary under a
        # tight byte budget must be truncated/dropped while recent context
        # (reserved) still makes it in.
        budget = build_groq_prompt_budget(
            seed_prompt="seed prompt",
            use_profile_glossary=True,
            active_profile="profile",
            last_transcript="recent context " * 20,
            glossary_builder=lambda profile: "term " * 100,
            max_context_chars=120,
            max_prompt_chars=80,
        )

        self.assertTrue(budget.glossary_present)
        self.assertTrue(budget.glossary_truncated)
        self.assertTrue(budget.context_present)
        self.assertTrue(budget.context_included)
        self.assertLessEqual(budget.prompt_bytes, 80)

    def test_prompt_budget_no_glossary_no_context(self):
        budget = build_groq_prompt_budget(
            seed_prompt="seed prompt",
            use_profile_glossary=False,
            active_profile="profile",
            last_transcript="",
            glossary_builder=lambda profile: "unused",
            max_context_chars=50,
        )

        self.assertFalse(budget.glossary_present)
        self.assertFalse(budget.glossary_truncated)
        self.assertFalse(budget.context_present)
        self.assertFalse(budget.context_included)

    def test_build_groq_prompt_keeps_hades_profile_under_groq_limit(self):
        from config import cfg
        from modules.streamer_profiles import build_stt_glossary

        prompt = build_groq_prompt(
            seed_prompt=cfg.stt.groq_prompt,
            use_profile_glossary=True,
            active_profile="hades_chxxnnx",
            last_transcript="최근 문맥 " * 50,
            glossary_builder=build_stt_glossary,
            max_context_chars=120,
            max_prompt_chars=896,
        )

        self.assertIsNotNone(prompt)
        self.assertLessEqual(len(prompt.encode("utf-8")), 896)
        self.assertIn("Korean gaming livestream speech", prompt)
        self.assertIn("Recent Korean transcript context:", prompt)

    def test_dedupe_transcript_overlap_removes_repeated_word_prefix(self):
        self.assertEqual(
            dedupe_transcript_overlap(
                "여기를 선택하면은 여기 기지를 우리가",
                "여기 기지를 우리가 이제 다같이 디펜스 해야 돼요",
            ),
            "이제 다같이 디펜스 해야 돼요",
        )

    def test_dedupe_transcript_overlap_removes_repeated_char_prefix(self):
        self.assertEqual(
            dedupe_transcript_overlap("안녕하세요반갑습니다", "반갑습니다오늘은"),
            "오늘은",
        )

    def test_dedupe_transcript_overlap_keeps_unrelated_text(self):
        current = "직업은 들어가서 하는 건가요"

        self.assertEqual(dedupe_transcript_overlap("벽을 세워야 돼요", current), current)


if __name__ == "__main__":
    unittest.main()
