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


if __name__ == "__main__":
    unittest.main()
