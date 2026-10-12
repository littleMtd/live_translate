"""Publication of copied-source-name candidates (docs/agent/LIVE_DROPPED_SUBTITLES_PLAN_20261010.md,
first-route stage: docs/agent/LIVE_PRIMARY_RESCUE_PLAN_20261012.md)."""

import unittest
from unittest.mock import MagicMock

import modules.translation_engines as translation_engines_module
import tests.test_translator as tt
from modules.translation_runtime import FallbackState, call_with_fallback
from modules.translator import (
    GUARD_RESCUE_POLICY,
    _choose_guard_rescue,
    _final_script_rejection_reason,
    _guard_rescue_eligible,
    _guard_rescue_text_ok,
    _outcome_used_api,
)

SOURCE = "뭐 이 정도? 야, 뽀뿌야!"
RESCUABLE = "什麼？就這程度？喂，뽀뿌！"


def evidence(restored, corrected, failed=(("unexpected_hangul", "script_safety"),)):
    return {
        "disposition": "rejected",
        "reason": failed[0][0] if failed else "",
        "failed_invariants": [{"reason": r, "owner": o} for r, o in failed],
        "candidate_stages": {"protection_restored": {"text": restored}},
        "candidate_raw_output": restored,
        "candidate_output": corrected,
    }


class RuleTests(unittest.TestCase):
    def test_copied_names_allowed_clauses_and_copies_refused(self):
        self.assertTrue(_guard_rescue_text_ok(RESCUABLE, SOURCE))
        self.assertTrue(_guard_rescue_text_ok("生日快樂，부가姐姐！主角。生日快樂！", "생일 축하해요 부가 언니! 주인공. 생일 축하해!"))
        self.assertTrue(_guard_rescue_text_ok("我做랑박사時做了十多個來吃耶？", "나 랑박사 할 때 열 개 넘게 만들어 먹었는데?"))
        refused = (
            ("공연 취소됐어요，今天的直播節目安排如下請各位觀眾準時收看。", "공연 취소됐어요."),  # copied clause
            ("오늘 공연 취소됐어요，今天的直播節目安排如下。", "오늘 공연 취소됐어요"),  # three runs
            ("양재사님 고마워요.", "양재사님 고마워요."),  # verbatim copy
            ("被징버거님無法形容的魅力吸引了。", "버거님의 형용할 수 없는 매력에 이끌렸습니다."),  # not in source
            ("什麼這樣？ 嘿，뽀뿌야！", SOURCE),  # Hangul share above 30 %
            ("김치찜남 챈나다 吃飯了嗎？", "김치찜남 챈나다 밥 먹었어?"),  # copula form
            ("말해，今天大家一起吃飯然後回家。", "말해"),  # 해 ending
            ("간다，今天大家一起吃飯然後回家。", "간다"),  # two-syllable 다
            ("먹고도，今天大家一起吃飯然後回家。", "먹고도"),  # 고 after stripping 도
            ("민지，今天大家一起吃飯然後回家。", "abc민지123 왔어"),  # token is not just 민지
            ("", SOURCE),
        )
        for text, source in refused:
            with self.subTest(text=text):
                self.assertFalse(_guard_rescue_text_ok(text, source))

    def test_v2_accepts_word_pieces_and_up_to_four_names(self):
        # 2026-10-11 replay of October lost sentences (docs/agent/LIVE_RESCUE_V2_PLAN_20261011.md).
        self.assertTrue(_guard_rescue_text_ok("牛奶，유천來了，鯽魚。", "우유 유천이 와우 붕어"))  # piece of 유천이
        self.assertTrue(_guard_rescue_text_ok("為了慶祝징버기生日想畫圖，看了징버기的翻唱曲後馬上照著畫了。",
                                              "징버기 생축 기념 그림 그리려다가 징버기 커버곡 보고 바로 따라 그렸습니다."))
        self.assertTrue(_guard_rescue_text_ok(
            "Happy 부가Day！生日快樂。啊，好可愛！像똥깡이抱枕一樣。버거直接寫就好，這裡寫得很清楚很清楚。",
            "해피 부가데이! 생일 축하합니다. 아 귀여워! 똥깡이 쿠션같이. 버거 그냥 적어"))  # three runs
        refused = (
            ("치說了，今天大家一起吃飯然後回家很開心。", "치치 소리가 들려서"),  # 1-syllable piece
            ("가유說了，今天大家一起吃飯然後回家很開心。", "나가 유천이 왔어"),  # spans two words
            ("가나，다라，마바，사아，자차，今天大家一起吃飯然後回家很開心很開心很開心。",
             "가나 다라 마바 사아 자차"),  # five runs
            ("유천이었說了，今天大家一起吃飯然後回家很開心。", "유천이었어 왔다"),  # piece ending like a predicate
            ("我先먹었습，今天大家一起吃飯然後回家很開心很開心。", "저는 먹었습니다 그리고"),  # piece of a predicate word
            ("畫得예쁘，今天大家一起吃飯然後回家很開心很開心。", "엄청 예쁘게 그려줬다"),  # adverbial -게 stem
            ("我좋아，今天大家一起吃飯然後回家很開心很開心。", "내가 좋아하는 음식"),  # adnominal 하는 stem
        )
        for text, source in refused:
            with self.subTest(text=text):
                self.assertFalse(_guard_rescue_text_ok(text, source))
        # exactly four distinct runs (each a source token or word piece) still qualify
        self.assertTrue(_guard_rescue_text_ok(
            "가나、다라、마바、사아這四位今天都來了，大家一起吃飯然後回家很開心很開心很開心很開心。",
            "가나 다라 마바 사아 왔어"))
        # a 2+ syllable piece of a mixed token qualifies; the token boundary is the whitespace word
        self.assertTrue(_guard_rescue_text_ok("뽀뿌說要一起去吃飯，大家都很開心很開心。", "abc뽀뿌123 같이 가자"))
        self.assertEqual(GUARD_RESCUE_POLICY, "copied_source_name_v2")
        # the piece restriction does not touch whole tokens: 징부기 (from 징부기가) still qualifies
        self.assertTrue(_guard_rescue_text_ok("只要징부기喜歡就好，今天大家一起吃飯然後回家很開心。", "그냥 징부기가 좋으면"))

    def test_short_names_that_look_like_endings_are_not_rescued(self):
        # The contract checks endings on every run; a name such as 민지 is simply not rescued (safe direction).
        self.assertFalse(_guard_rescue_text_ok("민지說要一起去吃飯，大家都很開心。", "민지가 같이 밥 먹으러 가자고 했어"))
        self.assertTrue(_guard_rescue_text_ok("뽀뿌說要一起去吃飯，大家都很開心。", "뽀뿌가 같이 밥 먹으러 가자고 했어"))

    def test_eligibility_needs_only_hangul_failures_and_both_stages(self):
        self.assertTrue(_guard_rescue_eligible(evidence(RESCUABLE, RESCUABLE), SOURCE))
        self.assertTrue(_guard_rescue_eligible(evidence(RESCUABLE, RESCUABLE,
                                                        (("raw_unexpected_hangul", "script_safety"),)), SOURCE))
        self.assertFalse(_guard_rescue_eligible(evidence(RESCUABLE, RESCUABLE,
                                                         (("unexpected_hangul", "script_safety"),
                                                          ("canonical_obligation_missing", "canonical_obligation"))), SOURCE))
        self.assertFalse(_guard_rescue_eligible(evidence(RESCUABLE, RESCUABLE, ()), SOURCE))
        # raw (protection-restored) stage had a non-source run that a correction later removed
        self.assertFalse(_guard_rescue_eligible(evidence("什麼？就這程度？喂，징버거！", RESCUABLE), SOURCE))
        missing_stage = evidence(RESCUABLE, RESCUABLE)
        missing_stage["candidate_stages"] = {}
        self.assertFalse(_guard_rescue_eligible(missing_stage, SOURCE))
        self.assertFalse(_guard_rescue_eligible({**evidence(RESCUABLE, RESCUABLE), "disposition": "accepted"}, SOURCE))

    def test_choose_prefers_least_hangul_then_earliest(self):
        attempts = (
            {"status": "empty"},
            {"status": "rejected_output", "output_guard": evidence("喂，뽀뿌！好啊好啊。", "喂，뽀뿌！好啊好啊。")},
            {"status": "rejected_output", "output_guard": evidence(RESCUABLE, RESCUABLE)},
            {"status": "rejected_output", "output_guard": evidence("공연 취소됐어요。", "공연 취소됐어요。")},
        )
        # index 1: 2 Hangul / 7 chars (28.6 %); index 2: 2 / 9 (22.2 %) -> the lower ratio wins
        self.assertEqual(_choose_guard_rescue(attempts, SOURCE)["attempt_index"], 2)
        tie = ({"status": "rejected_output", "output_guard": evidence(RESCUABLE, RESCUABLE)},
               {"status": "rejected_output", "output_guard": evidence(RESCUABLE, RESCUABLE)})
        self.assertEqual(_choose_guard_rescue(tie, SOURCE)["attempt_index"], 0)
        self.assertIsNone(_choose_guard_rescue(({"status": "empty"},), SOURCE))


class RuntimeHookTests(unittest.TestCase):
    def call(self, engines, rescue):
        return call_with_fallback(engines, FallbackState(), SOURCE, "system", False, None, 3,
                                  lambda *_a: False, MagicMock(), return_details=True,
                                  output_guard=lambda engine, candidate, _src: (
                                      evidence(candidate, candidate) if "뽀뿌" in candidate else {}),
                                  output_rescue=rescue)

    def test_hook_not_called_on_success(self):
        translation_engines_module.reset_translation_call_trace()
        hook = MagicMock(return_value=None)
        result, _idx, _details = self.call([tt._route_engine("deepseek", "什麼？就這程度？")], hook)
        self.assertEqual(result, "什麼？就這程度？")
        hook.assert_not_called()

    def test_rejected_then_empty_is_rescued_and_marked(self):
        translation_engines_module.reset_translation_call_trace()
        engines = [tt._route_engine("deepseek", RESCUABLE), tt._route_engine("groq", "")]
        result, idx, details = self.call(engines, lambda attempts: _choose_guard_rescue(attempts, SOURCE))
        self.assertEqual((result, idx), (RESCUABLE, 0))
        self.assertEqual(details.selected_attempt["status"], "rejected_output")
        self.assertEqual(details.selected_attempt["guard_rescue"]["policy"], GUARD_RESCUE_POLICY)
        self.assertTrue(details.selected_attempt["selected_for_output"])

    def test_first_route_copied_name_is_published_without_asking_the_fallback(self):
        # docs/agent/LIVE_PRIMARY_RESCUE_PLAN_20261012.md
        translation_engines_module.reset_translation_call_trace()
        groq = tt._route_engine("groq", "什麼？就這程度？喂，波布！")
        engines = [tt._route_engine("deepseek", RESCUABLE), groq]
        result, idx, details = self.call(engines, lambda attempts: _choose_guard_rescue(attempts, SOURCE))
        self.assertEqual((result, idx), (RESCUABLE, 0))
        self.assertEqual(details.selected_attempt["guard_rescue"]["stage"], "first_route")
        self.assertEqual(len(details.attempts), 1)
        groq.translate.assert_not_called()
        groq.translate_messages.assert_not_called()

    def test_ineligible_first_route_still_falls_back_then_rescues_last(self):
        translation_engines_module.reset_translation_call_trace()
        copied_clause = "뭐 이 정도? 야，뽀뿌就這樣。"
        engines = [tt._route_engine("deepseek", copied_clause), tt._route_engine("groq", RESCUABLE)]
        result, idx, details = self.call(engines, lambda attempts: _choose_guard_rescue(attempts, SOURCE))
        self.assertEqual((result, idx), (RESCUABLE, 1))
        self.assertEqual(details.selected_attempt["guard_rescue"]["stage"], "last_resort")
        self.assertEqual(len(details.attempts), 2)

    def test_provider_exception_still_raises_without_rescue(self):
        engine = tt._route_engine("deepseek", RESCUABLE)
        engine.translate_messages.side_effect = RuntimeError("boom")
        engine.translate.side_effect = RuntimeError("boom")
        hook = MagicMock()
        with self.assertRaises(RuntimeError):
            self.call([engine], hook)
        hook.assert_not_called()


class EndToEndTests(unittest.TestCase):
    def translate(self, *returns):
        translator = tt._make_translator()
        translator._engines = [tt._route_engine(name, value) for name, value in zip(("deepseek", "groq"), returns)]
        translation_engines_module.reset_translation_call_trace()
        with tt._active_translation_profile("isegye_lilpa"):
            return translator, translator.translate_event(SOURCE, False)

    def test_all_rejected_copied_name_is_published_not_cached(self):
        translator, outcome = self.translate(RESCUABLE, "")
        self.assertEqual(outcome.status, "success", outcome.filter_reason)
        self.assertEqual(outcome.result_source, "guard_rescue")
        self.assertEqual(outcome.target_text, RESCUABLE)
        self.assertEqual(outcome.guard_rescue["kept_hangul"], ["뽀뿌"])
        self.assertTrue(_outcome_used_api(outcome))
        fields = outcome.as_event_fields(12.0, {})
        self.assertIn("published_with_source_hangul", fields["quality_flags"])
        self.assertEqual(fields["guard_rescue"]["policy"], GUARD_RESCUE_POLICY)
        self.assertEqual(_final_script_rejection_reason(fields), "")
        self.assertEqual(_final_script_rejection_reason({**fields, "target_text": "公演 공연 취소됐어요"}), "unexpected_hangul")

    def test_rescued_result_never_reaches_cache(self):
        translator = tt._make_translator()
        translator._engines = [tt._route_engine("deepseek", RESCUABLE)]
        store, remember, transcript = MagicMock(), MagicMock(), MagicMock()
        translator._memory.cache_store = store
        translator._memory.db_store = store
        translator._memory.write_history = transcript
        translator._history_state().remember = remember
        translation_engines_module.reset_translation_call_trace()
        with tt._active_translation_profile("isegye_lilpa"):
            outcome = translator.translate_event(SOURCE, False)
        if outcome.deferred_success:
            outcome.deferred_success()
        self.assertEqual(outcome.result_source, "guard_rescue")
        store.assert_not_called()
        remember.assert_called_once()
        transcript.assert_called_once_with(SOURCE, RESCUABLE)

    def test_copied_name_from_primary_wins_over_a_clean_fallback(self):
        _translator, outcome = self.translate(RESCUABLE, "什麼？就這程度？喂，你啊！")
        self.assertEqual((outcome.status, outcome.result_source, outcome.engine), ("success", "guard_rescue", "deepseek"))
        self.assertEqual(outcome.target_text, RESCUABLE)
        self.assertEqual(outcome.guard_rescue["stage"], "first_route")

    def test_ineligible_primary_then_clean_fallback_keeps_normal_path(self):
        _translator, outcome = self.translate("뭐 이 정도? 야，就這樣。", "什麼？就這程度？喂，你啊！")
        self.assertEqual((outcome.status, outcome.result_source, outcome.engine), ("success", "api", "groq"))

    def test_ineligible_candidates_still_fail(self):
        translator = tt._make_translator()
        translator._engines = [tt._route_engine("deepseek", "뭐 이 정도? 야，就這樣。")]
        translation_engines_module.reset_translation_call_trace()
        with tt._active_translation_profile("isegye_lilpa"):
            outcome = translator.translate_event(SOURCE, False)
        self.assertEqual(outcome.status, "failed")
        self.assertNotEqual(outcome.result_source, "guard_rescue")


class RepetitionTests(unittest.TestCase):
    def reason(self, source, target):
        from modules.translator import _adjudicate_translation_candidate
        with tt._active_translation_profile("isegye_lilpa"):
            return _adjudicate_translation_candidate(tt._route_engine("deepseek", target), target, source).reason

    def test_repetition_that_mirrors_the_source_is_not_degenerate(self):
        self.assertEqual(self.reason("괜찮아, 괜찮아, 괜찮아", "沒關係，沒關係，沒關係"), "")
        self.assertEqual(self.reason("오 마이 갓! 오 마이 갓! 오 마이 갓! 안 돼. 안 돼. 올라와야 돼. 올라와야 돼. 올라와야 돼.",
                                     "我的天！我的天！我的天！不行。不行。得爬上來。得爬上來。得爬上來。"), "")

    def test_looping_output_for_an_ordinary_or_much_shorter_source_is_still_rejected(self):
        self.assertEqual(self.reason("오늘 방송은 여기까지 하겠습니다", "今天今天今天今天今天今天今天今天"), "repetitive_target")
        self.assertEqual(self.reason("괜찮아, 괜찮아, 괜찮아", "沒關係，" * 8), "repetitive_target")
        # punctuation runs do not make a source repetitive; a longer loop than the source is not faithful
        self.assertEqual(self.reason("와!!!!!!!!!! 대박!!!!!!!!!!", "哇！" + "太棒了" * 6), "repetitive_target")
        self.assertEqual(self.reason("아니 아니 아니 아니", "不" * 16), "repetitive_target")
        self.assertEqual(self.reason("lalalalalalala", "啦" * 19), "repetitive_target")

    def test_first_route_rescue_leaves_fallback_state_alone(self):
        for active_idx in (0, 1):
            with self.subTest(active_idx=active_idx):
                translation_engines_module.reset_translation_call_trace()
                state = FallbackState()
                state.active_idx = active_idx
                state.consecutive_primary_failures = 1
                state.primary_cooldown_until = 123.0
                engines = [tt._route_engine("deepseek", RESCUABLE), tt._route_engine("groq", RESCUABLE)]
                result, idx, details = call_with_fallback(
                    engines, state, SOURCE, "system", False, None, 3, lambda *_a: False, MagicMock(),
                    return_details=True, circuit_breaker_enabled=True,
                    output_guard=lambda _engine, candidate, _src: evidence(candidate, candidate),
                    output_rescue=lambda attempts: _choose_guard_rescue(attempts, SOURCE))
                self.assertEqual((result, idx), (RESCUABLE, active_idx))
                self.assertEqual(details.selected_attempt["guard_rescue"]["stage"], "first_route")
                self.assertEqual((state.active_idx, state.consecutive_primary_failures, state.primary_cooldown_until),
                                 (active_idx, 1, 123.0))


if __name__ == "__main__":
    unittest.main()


class FinalizerTrustTests(unittest.TestCase):
    def test_forged_hint_is_rejected_by_publication_adjudication(self):
        from modules.request_protection import resolve_request_protection
        translator = tt._make_translator()
        engine = tt._route_engine("deepseek", "")
        with tt._active_translation_profile("isegye_lilpa"):
            outcome = translator._finalize_translation_result(
                raw_text=SOURCE, prepared_text=SOURCE, provider_result="뭐 이 정도? 야，就這樣。",
                promoted=False, engine=engine, prompt_version="v", cache_status="miss", incomplete=False,
                canonical_obligations=(), request_protection=resolve_request_protection(SOURCE),
                history_cohort=translator._history_cohort(),
                guard_rescue={"policy": GUARD_RESCUE_POLICY, "attempt_index": 0})
        self.assertEqual(outcome.status, "failed")
        self.assertNotEqual(outcome.result_source, "guard_rescue")
