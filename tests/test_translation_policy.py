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

    # ---- STT template hallucination guard (#8) ----

    def test_hard_phrase_with_ad_keyword_stays_stt_garbage(self):
        # Order is `... → stt_garbage → stt_template_garbage`. This sample has
        # no `?`/`!` and contains `광고`, so the existing commercial-keyword
        # rule fires first — must keep its original reason (no rename).
        policy = TranslationPolicy(slang={})

        self.assertEqual(
            policy.rejection_reason("자막 제공 및 광고를 포함하고 있습니다."),
            "stt_garbage",
        )

    def test_hard_template_leaking_sample_is_rejected(self):
        # Trailing `!` defeats the `'!' not in text` guard of the commercial
        # keyword rule, so this currently leaks — the hard template phrase
        # must catch it as stt_template_garbage.
        policy = TranslationPolicy(slang={})

        self.assertEqual(
            policy.rejection_reason("자막 제공 및 자막 제공 및 광고를 포함하고 있습니다!"),
            "stt_template_garbage",
        )
        # §10.2-A slen=110 leaking sample (trailing `!`, no single-word repeat).
        self.assertEqual(
            policy.rejection_reason(
                "자막 제공 및 자막 제공 및 광고를 포함하고 있습니다. "
                "자막 제공 및 광고를 포함하고 있습니다. "
                "자막 제공 및 광고를 포함하고 있습니다. "
                "자막 제공 및 광고를 포함하고 있습니다. 양지인 사람이잖아!"
            ),
            "stt_template_garbage",
        )

    def test_isolated_outro_template_is_rejected(self):
        policy = TranslationPolicy(slang={})

        self.assertEqual(
            policy.rejection_reason("시청해주셔서 감사합니다."),
            "stt_template_garbage",
        )

    def test_repeated_conditional_template_is_rejected(self):
        policy = TranslationPolicy(slang={})

        self.assertEqual(
            policy.rejection_reason("시청해주셔서 감사합니다. 시청해주셔서 감사합니다."),
            "stt_template_garbage",
        )

    def test_subscribe_like_full_template_is_rejected(self):
        policy = TranslationPolicy(slang={})

        self.assertEqual(
            policy.rejection_reason("구독과 좋아요는 저에게 아주 큰 힘이 됩니다."),
            "stt_template_garbage",
        )

    def test_genuine_short_thanks_is_not_rejected(self):
        # Real streamer thanking subs — must NOT be blocked.
        policy = TranslationPolicy(slang={})

        self.assertIsNone(policy.rejection_reason("오! 구독 감사합니다!"))
        self.assertIsNone(policy.rejection_reason("구독 감사합니다."))

    def test_real_content_with_template_tail_is_not_rejected(self):
        # Real speech + a template tail: remainder is large, ratio low → keep.
        policy = TranslationPolicy(slang={})

        self.assertIsNone(
            policy.rejection_reason(
                "여러분들 소리 들려요? 오케이. 굿. 구독과 좋아요는 저에게 큰 힘이 됩니다."
            )
        )

    def test_stt_template_garbage_does_not_update_last_input(self):
        # A fabricated template must NOT poison the duplicate slot.
        policy = TranslationPolicy(slang={})

        self.assertIsNone(policy.prepare_input("시청해주셔서 감사합니다."))
        self.assertEqual(policy.last_input, "")
        # The next identical input is still classified as template garbage,
        # NOT silently swallowed as `duplicate`.
        self.assertEqual(
            policy.rejection_reason("시청해주셔서 감사합니다."),
            "stt_template_garbage",
        )

    def test_existing_stt_garbage_positives_still_stt_garbage(self):
        # §10.2-B regression: previously-blocked positives keep `stt_garbage`,
        # the order must not rename existing behavior.
        policy = TranslationPolicy(slang={})
        for sample in (
            "자막 제공 및 자막 제공 및 광고를 포함하고 있습니다. "
            "시청해주셔서 감사합니다. 시청해주셔서 감사합니다.",
            "한글자막 제공 및 자막 제공 및 광고를 포함하고 있습니다.",
            "자막 제공 및 광고를 포함하고 있습니다.",
            "자막 제공 및 광고는 kakaotalk 플러스친구의 "
            "홈페이지에서 확인하실 수 있습니다.",
        ):
            self.assertEqual(
                policy.rejection_reason(sample), "stt_garbage", msg=sample
            )
