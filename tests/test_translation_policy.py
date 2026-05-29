import unittest

from config import cfg
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

    def test_slang_result_uses_exact_match_for_global_glossary(self):
        policy = TranslationPolicy(slang=cfg.translation.slang)

        self.assertEqual(policy.slang_result("마크"), "Minecraft")
        self.assertEqual(policy.slang_result("섭주"), "服主")
        self.assertEqual(policy.slang_result("섭쥬방"), "服主房")
        self.assertIsNone(policy.slang_result("마크 서버"))

    def test_is_stt_garbage_detects_repetition(self):
        self.assertTrue(TranslationPolicy.is_stt_garbage("하하 하하 하하 정상"))

    def test_is_stt_garbage_allows_short_text(self):
        self.assertFalse(TranslationPolicy.is_stt_garbage("안녕하세요"))

    def test_is_stt_garbage_allows_weak_commercial_words_in_normal_speech(self):
        self.assertFalse(
            TranslationPolicy.is_stt_garbage(
                "아무튼, 어머머머머머. 그래서 이분께... 이 노래 추천드립니다. 그렇고 그런 사이."
            )
        )
        self.assertFalse(
            TranslationPolicy.is_stt_garbage(
                "신나는 아침이구먼. 여러분들 뭔가 이거 듣고 다들 구매하신 초식 다"
            )
        )

    def test_is_stt_garbage_allows_adjective_ad_use_in_real_speech(self):
        self.assertFalse(
            TranslationPolicy.is_stt_garbage(
                "부채 있잖아요. 주류 회사 광고용으로. 그런 부채였는데 기념품으로 둘이 가져가더라고요."
            )
        )

    def test_is_stt_garbage_still_rejects_strong_commercial_words(self):
        self.assertTrue(
            TranslationPolicy.is_stt_garbage("사이트 들어가보세요 구매 클릭 방문")
        )
        self.assertTrue(
            TranslationPolicy.is_stt_garbage("자막 제공 및 광고를 포함하고 있습니다.")
        )

    def test_is_stt_garbage_allows_korean_game_click_terms(self):
        self.assertFalse(
            TranslationPolicy.is_stt_garbage(
                "에메랄드 블럭을 우클릭하면 기지가 활성화되면서 상점이 생깁니다."
            )
        )

    def test_is_stt_garbage_allows_common_game_acronyms(self):
        self.assertFalse(
            TranslationPolicy.is_stt_garbage(
                "마인크래프트 RPG 서버입니다. PVP랑 PVE 요소도 조금 있어요."
            )
        )

    def test_is_stt_garbage_allows_single_short_english_acronym(self):
        self.assertFalse(
            TranslationPolicy.is_stt_garbage(
                "제가 며칠 전에 KFC에 들렀는데 우연찮게 제 첫 길보드가 나오는 게 아니겠습니까?"
            )
        )
        self.assertFalse(
            TranslationPolicy.is_stt_garbage(
                "어제 LCK 미드자이라도 나왔었음. 결과가 안 좋았지만 모친이라는 다른 말."
            )
        )
        self.assertFalse(
            TranslationPolicy.is_stt_garbage(
                "아 지금 타이밍 좋긴 한데 돌아가 볼게 아니아니아니 힐링 RPG 중입니다."
            )
        )

    def test_is_stt_garbage_still_rejects_many_short_english_fragments(self):
        self.assertTrue(
            TranslationPolicy.is_stt_garbage("ABC DEF GHI 같은 이상한 한국어 조각")
        )

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
        self.assertEqual(
            policy.rejection_reason("구독과 좋아요 부탁드립니다!"),
            "stt_template_garbage",
        )
        self.assertEqual(
            policy.rejection_reason("구독과 좋아요 부탁 드립니다."),
            "stt_template_garbage",
        )
        self.assertEqual(
            policy.rejection_reason("구독, 좋아요, 알림설정 부탁드립니다."),
            "stt_template_garbage",
        )
        self.assertEqual(
            policy.rejection_reason("좋아요랑 구독 부탁해요!"),
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


class TestSttTemplateFragmentSanitizer(unittest.TestCase):
    def test_strip_leading_conditional_template(self):
        self.assertEqual(
            TranslationPolicy.strip_stt_template_fragments(
                "시청해주셔서 감사합니다. 엄청나게 그렇잖아."
            ),
            "엄청나게 그렇잖아.",
        )

    def test_strip_trailing_conditional_template(self):
        self.assertEqual(
            TranslationPolicy.strip_stt_template_fragments(
                "글씨는 영어시스템을 사용하여 사용하였습니다. 시청해주셔서 감사합니다."
            ),
            "글씨는 영어시스템을 사용하여 사용하였습니다.",
        )

    def test_strip_leading_subscribe_template(self):
        self.assertEqual(
            TranslationPolicy.strip_stt_template_fragments(
                "구독과 좋아요는 저에게 아주 큰 힘이 됩니다. 댓글로 남겨주세요!"
            ),
            "댓글로 남겨주세요!",
        )
        self.assertEqual(
            TranslationPolicy.strip_stt_template_fragments(
                "구독과 좋아요 부탁 어? 진짜? 카페에 챗나룩 서버 포스터 누가 큐티 버전으로 올려주셨다고요?"
            ),
            "어? 진짜? 카페에 챗나룩 서버 포스터 누가 큐티 버전으로 올려주셨다고요?",
        )
        self.assertEqual(
            TranslationPolicy.strip_stt_template_fragments(
                "구독과 좋아요는 저 이제 2집 녹음하러 가거든요? 여러분들?"
            ),
            "저 이제 2집 녹음하러 가거든요? 여러분들?",
        )
        self.assertEqual(
            TranslationPolicy.strip_stt_template_fragments(
                "구독과 좋아요 부탁 드립니다. 야, 샌 몬스터 죽이면 돈 더 많이 줘?"
            ),
            "야, 샌 몬스터 죽이면 돈 더 많이 줘?",
        )
        self.assertEqual(
            TranslationPolicy.strip_stt_template_fragments(
                "구독, 좋아요, 알림 이거 안되네? 야야 좋다 포탑 올빵하니까 좋은데?"
            ),
            "이거 안되네? 야야 좋다 포탑 올빵하니까 좋은데?",
        )

    def test_strip_leading_hard_template_prefix(self):
        self.assertEqual(
            TranslationPolicy.strip_stt_template_fragments(
                "자막 제공 및 자막 제공 및 광고를 포함하고 있습니다. 우와! 너무 예쁘던데?"
            ),
            "우와! 너무 예쁘던데?",
        )
        self.assertEqual(
            TranslationPolicy.strip_stt_template_fragments(
                "자막 제공 및 자막 제공 및 광고를 포함하고 있습니다. "
                "자막 제공 및 광고를 포함하고 있습니다. 우와! 너무 예쁘던데?"
            ),
            "우와! 너무 예쁘던데?",
        )

    def test_strip_repeated_leading_template(self):
        self.assertEqual(
            TranslationPolicy.strip_stt_template_fragments(
                "시청해주셔서 감사합니다. 시청해주셔서 감사합니다. 진짜내용."
            ),
            "진짜내용.",
        )

    def test_strip_internal_template_sentence(self):
        self.assertEqual(
            TranslationPolicy.strip_stt_template_fragments(
                "따아 이거 진짜 위기상황 시청해주셔서 감사합니다. 근데 너네 이거 다 알아?"
            ),
            "따아 이거 진짜 위기상황 근데 너네 이거 다 알아?",
        )

    def test_partial_internal_thanks_is_not_removed(self):
        text = "우리 채널에 시청해주셔서 감사하다는 댓글이 많아요!"

        self.assertEqual(TranslationPolicy.strip_stt_template_fragments(text), text)

    def test_strip_to_empty_returns_none(self):
        self.assertIsNone(
            TranslationPolicy.strip_stt_template_fragments("시청해주셔서 감사합니다.")
        )

    def test_strip_leaves_no_hangul_returns_none(self):
        self.assertIsNone(
            TranslationPolicy.strip_stt_template_fragments("시청해주셔서 감사합니다. ok!")
        )

    def test_pure_template_still_rejected_by_guard_before_sanitizer(self):
        policy = TranslationPolicy(slang={})

        self.assertEqual(
            policy.rejection_reason("시청해주셔서 감사합니다."),
            "stt_template_garbage",
        )

    def test_sanitized_last_input_prevents_duplicate(self):
        policy = TranslationPolicy(slang={})

        self.assertEqual(
            policy.prepare_input("시청해주셔서 감사합니다. 진짜 내용입니다."),
            "진짜 내용입니다.",
        )
        self.assertIsNone(policy.prepare_input("진짜 내용입니다."))

    def test_strip_trailing_hard_template_keeps_real_prefix(self):
        self.assertEqual(
            TranslationPolicy.strip_stt_template_fragments(
                "입주비는 안 받습니다. 저희 스폰서분들도 너무 감사하게도 좀 많이 붙어가지고. "
                "자막 제공 및 자막 제공 및 광고를 포함하고 있습니다. "
                "자막 제공 및 광고를 포함하고 있습니다."
            ),
            "입주비는 안 받습니다. 저희 스폰서분들도 너무 감사하게도 좀 많이 붙어가지고",
        )

    def test_prepare_input_rescues_hard_template_tail_before_stt_garbage(self):
        policy = TranslationPolicy(slang={})

        sanitized = policy.prepare_input(
            "입주비는 안 받습니다. 저희 스폰서분들도 너무 감사하게도 좀 많이 붙어가지고. "
            "자막 제공 및 자막 제공 및 광고를 포함하고 있습니다."
        )

        self.assertEqual(
            sanitized,
            "입주비는 안 받습니다. 저희 스폰서분들도 너무 감사하게도 좀 많이 붙어가지고",
        )
        self.assertEqual(policy.last_input, sanitized)

    def test_hard_template_with_real_tail_is_stripped(self):
        policy = TranslationPolicy(slang={})
        text = "자막 제공 및 광고를 포함하고 있습니다. 좋아 좋아"

        self.assertEqual(TranslationPolicy.strip_stt_template_fragments(text), "좋아 좋아")
        self.assertEqual(policy.prepare_input(text), "좋아 좋아")

    def test_hard_template_inside_real_speech_is_stripped(self):
        policy = TranslationPolicy(slang={})
        text = (
            "피망업 노노노 자막 제공 및 자막 제공 및 광고를 포함하고 있습니다. "
            "거짓말 하지마! 저희는 돈이 없어요."
        )

        expected = "피망업 노노노 거짓말 하지마! 저희는 돈이 없어요."
        self.assertEqual(TranslationPolicy.strip_stt_template_fragments(text), expected)
        self.assertEqual(policy.prepare_input(text), expected)

    def test_repeated_conditional_template_with_real_tail_is_sanitized(self):
        policy = TranslationPolicy(slang={})

        self.assertEqual(
            policy.prepare_input("시청해주셔서 감사합니다. 시청해주셔서 감사합니다. 팻으로 넣어드려요? 몬스터!"),
            "팻으로 넣어드려요? 몬스터!",
        )


class TestSttLowValueFragmentGuard(unittest.TestCase):
    def test_pure_low_value_fragment_is_rejected_without_last_input_update(self):
        policy = TranslationPolicy(slang={})
        text = "도도리코 소라에 타받세 도개가 사라지게 된 날"

        self.assertEqual(policy.rejection_reason(text), "stt_low_value_fragment")
        self.assertIsNone(policy.prepare_input(text))
        self.assertEqual(policy.last_input, "")

    def test_low_value_song_tail_is_stripped_when_prefix_is_useful(self):
        policy = TranslationPolicy(slang={})
        text = "그걸 직접 만들 수 있다고? 너무 기대돼 망간부 바카스탕 골라요"

        self.assertEqual(
            policy.prepare_input(text),
            "그걸 직접 만들 수 있다고? 너무 기대돼",
        )

    def test_low_value_status_tail_is_stripped_when_prefix_is_useful(self):
        policy = TranslationPolicy(slang={})
        text = "잘 돼가시나요? 잘 돼가시나요 여러분? 저 오지키 움직이는 오지키 망간부 띵띵이가 움직인다"

        self.assertEqual(
            policy.prepare_input(text),
            "잘 돼가시나요? 잘 돼가시나요 여러분? 저",
        )

    def test_normal_song_request_is_not_rejected(self):
        policy = TranslationPolicy(slang={})

        self.assertIsNone(
            policy.rejection_reason("락 락 락 한 번 락 락 락 좋긴 해. 락 하나 넣는 거 좋긴 해. 뭐 있을까?")
        )

    def test_single_unknown_title_like_fragment_is_not_rejected(self):
        policy = TranslationPolicy(slang={})

        self.assertIsNone(policy.rejection_reason("매직 카펠라이드? 이렇게 멋진 파란 나를"))


class TestSttSongFragmentGuard(unittest.TestCase):
    def test_song_like_repeated_vocables_are_rejected(self):
        policy = TranslationPolicy(slang={})

        self.assertEqual(
            policy.rejection_reason("아아... 마음이... 21, 25? 라라 라라 노래 제목이 조금 이상한데?"),
            "stt_song_fragment",
        )

    def test_lyrics_fragment_with_song_context_is_rejected(self):
        policy = TranslationPolicy(slang={})

        self.assertEqual(
            policy.rejection_reason("쓰읍... 락이라면서 띵시렁띵시렁 흐흐흐 나는 아름다운 남의 날개를"),
            "stt_song_fragment",
        )

    def test_clear_singing_comment_is_not_rejected(self):
        policy = TranslationPolicy(slang={})

        self.assertIsNone(policy.rejection_reason("오늘 노래 진짜 잘 불렀어요"))

    def test_normal_excited_repetition_is_not_song_fragment(self):
        policy = TranslationPolicy(slang={})

        self.assertIsNone(policy.rejection_reason("아 진짜 진짜 너무 재밌었어요 여러분"))

    def test_repeated_game_exclamation_is_not_song_fragment(self):
        policy = TranslationPolicy(slang={})
        text = (
            "\uc544 \ub290\ub824. \uc544 \ub290\ub824. "
            "\uc544\ub2c8 \ub108\ubb34 \ub9ce\uc544 \ub108\ubb34 \ub9ce\uc544. "
            "\ubcd1 \ubb34\uc2dc\ud558\uc9c0 \uc7e4\ub124? \uc544\uc544! \uc544\uc544!"
        )

        self.assertIsNone(policy.rejection_reason(text))

    def test_screamed_game_request_is_not_song_fragment(self):
        policy = TranslationPolicy(slang={})
        text = (
            "\ud53c\ub0b4\ub098\uc694! \ud53c \ud55c\ubc88\ub9cc "
            "\uc7ac\uc6cc\uc918! \uc544\uc544\uc544\uc544\uc544\uc544\uc544\uc544\uc544! "
            "\uc544 \ub410\ub2e4 \ub410\ub2e4. \uc544 \ubb50\uc57c \uac15\ud654, "
            "\uc544 \ud06c\ub9ac\ud37c\ub124\uc774\ud130!"
        )

        self.assertIsNone(policy.rejection_reason(text))
