"""Last-resort publication of copied-source-name candidates (docs/agent/LIVE_DROPPED_SUBTITLES_PLAN_20261010.md)."""

import unittest
from unittest.mock import MagicMock

import modules.translation_engines as translation_engines_module
import tests.test_translator as tt
from modules.translation_runtime import FallbackState, call_with_fallback
from modules.translator import (
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

    def test_hook_not_called_on_success_and_used_only_at_terminal_failure(self):
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
        self.assertEqual(details.selected_attempt["guard_rescue"]["policy"], "copied_source_name_v1")
        self.assertTrue(details.selected_attempt["selected_for_output"])

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
        self.assertEqual(fields["guard_rescue"]["policy"], "copied_source_name_v1")
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

    def test_rejected_then_clean_fallback_keeps_normal_path(self):
        _translator, outcome = self.translate(RESCUABLE, "什麼？就這程度？喂，你啊！")
        self.assertEqual((outcome.status, outcome.result_source, outcome.engine), ("success", "api", "groq"))

    def test_ineligible_candidates_still_fail(self):
        translator = tt._make_translator()
        translator._engines = [tt._route_engine("deepseek", "뭐 이 정도? 야，就這樣。")]
        translation_engines_module.reset_translation_call_trace()
        with tt._active_translation_profile("isegye_lilpa"):
            outcome = translator.translate_event(SOURCE, False)
        self.assertEqual(outcome.status, "failed")
        self.assertNotEqual(outcome.result_source, "guard_rescue")


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
                guard_rescue={"policy": "copied_source_name_v1", "attempt_index": 0})
        self.assertEqual(outcome.status, "failed")
        self.assertNotEqual(outcome.result_source, "guard_rescue")
