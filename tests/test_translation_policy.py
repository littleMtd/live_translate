import unittest

from modules.translation_policy import TranslationPolicy


class TestTranslationPolicy(unittest.TestCase):
    def test_prepare_input_strips_text(self):
        policy = TranslationPolicy(slang={})

        self.assertEqual(policy.prepare_input("  안녕하세요  "), "안녕하세요")

    def test_prepare_input_suppresses_consecutive_duplicate(self):
        policy = TranslationPolicy(slang={})

        self.assertEqual(policy.prepare_input("안녕하세요"), "안녕하세요")
        self.assertIsNone(policy.prepare_input("안녕하세요"))

    def test_reset_last_input_allows_retry(self):
        policy = TranslationPolicy(slang={})

        policy.prepare_input("안녕하세요")
        policy.reset_last_input()

        self.assertEqual(policy.prepare_input("안녕하세요"), "안녕하세요")

    def test_prepare_input_rejects_short_text(self):
        policy = TranslationPolicy(slang={}, min_translate_chars=2)

        self.assertIsNone(policy.prepare_input("a"))

    def test_slang_result_returns_configured_translation(self):
        policy = TranslationPolicy(slang={"ㄱㄱ": "走吧"})

        self.assertEqual(policy.slang_result("ㄱㄱ"), "走吧")
        self.assertIsNone(policy.slang_result("없음"))

    def test_is_stt_garbage_detects_repetition(self):
        self.assertTrue(TranslationPolicy.is_stt_garbage("하하 하하 하하 정상"))

    def test_is_stt_garbage_allows_short_text(self):
        self.assertFalse(TranslationPolicy.is_stt_garbage("안녕하세요"))

    # ---- max_translate_chars (#6) ----

    def test_rejection_reason_returns_too_long_for_oversized_input(self):
        policy = TranslationPolicy(slang={}, max_translate_chars=10)

        self.assertEqual(policy.rejection_reason("x" * 11), "too_long")
        self.assertIsNone(policy.rejection_reason("x" * 10))

    def test_prepare_input_rejects_oversized_input(self):
        policy = TranslationPolicy(slang={}, max_translate_chars=10)

        self.assertIsNone(policy.prepare_input("x" * 11))

    def test_too_long_does_not_update_last_input(self):
        # An oversized input must NOT poison last_input, otherwise a subsequent
        # legitimate input matching it would be silently dropped as `duplicate`.
        policy = TranslationPolicy(slang={}, max_translate_chars=10)

        policy.prepare_input("x" * 11)

        self.assertEqual(policy.last_input, "")
