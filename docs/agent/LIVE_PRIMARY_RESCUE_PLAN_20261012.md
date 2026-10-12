# Live: publish the first route's copied-name candidate; stop rejecting faithful repetition (2026-10-12)

Author: Claude Code. Status: implemented; independent review recorded in §6.
Scope: `call_with_fallback` in `modules/translation_runtime.py`, the `repetitive_target` rejection in
`_adjudicate_translation_candidate` (`modules/translator.py`), `tests/test_guard_rescue.py`.
Unchanged: the output guard's Hangul rules, the rescue text rule (`copied_source_name_v2`), rescue
eligibility, the finalizer re-adjudication, history/cache handling, the circuit breaker, prompts.

## 1. Evidence (October live runs, `logs/runtime_events_202610*.jsonl`, no API calls)

Of 3,311 translated sentences, 111 published nothing and 79 were published by the Groq fallback.

- **Copied names.** In 28 sentences DeepSeek's candidate was rejected only for Hangul that the
  current rescue rule accepts (source names copied verbatim), and Groq's output was then published.
  Groq's version invented a transliteration or spelling (`띵씨` → 丁先生, `뿌잉` → 布汀, `김징징` → 金靜靜,
  `러비놀` → LoveNol), mistranslated the token (`라일락` → 薰衣草, `케미` → 化學反應), dropped it, or was
  truncated (`我在憂`). The extra route cost a median 1.07 s.
- **Repetition.** All 15 `repetitive_target` rejections (DeepSeek only) had a source that repeats by
  the same measure or is scat singing (`괜찮아, 괜찮아, 괜찮아`, `오 마이 갓! ×3`). Groq, which is not
  subject to the check, published the same repetition in 11 of them about 0.9 s later; 4 were lost.
  No rejection in October was a model loop on an ordinary source.

## 2. Change

1. **First-route rescue.** When the first route's candidate is rejected for content and the existing
   rescue hook accepts that single attempt, it is published at once (`guard_rescue.stage =
   "first_route"`), without calling later routes. The last-resort rescue over all attempts stays
   (`stage = "last_resort"`). Provider failures, empty output and ineligible rejections fall back as
   before. Metrics: `translation.guard_rescue.selected.{first_route,last_resort}`.
2. **Repetition.** `repetitive_target` no longer rejects a DeepSeek candidate when the source's own
   distinct-bigram ratio is below 0.5 (the flag's threshold) and the target is at most 1.25× the
   source, both measured with whitespace and punctuation removed. The quality flag is still recorded.

## 3. Trade-off

Rescue used to run only when every route failed; now a clean fallback output can lose to the
primary's Hangul-keeping one. The rule cannot tell an unknown nickname from a common noun the model
left untranslated (LIVE_RESCUE_V2_PLAN_20261011.md §3), so such a word is now shown in Hangul even
when the fallback would have translated it. In the 28 real cases this did not occur in a way the
judge preferred (§4).

## 4. Validation

- Gate set before judging: the DeepSeek candidate at least as good in ≥ 75 % and clearly worse in
  ≤ 15 % of the 28 pairs. Blind A/B (order randomized per item) by an independent Claude subagent
  that saw only source and the two candidates, with the house policy (unknown names stay in Hangul;
  untranslated ordinary words, invented transliterations, omissions and truncation are errors):
  **DeepSeek better 27, tie 1, Groq better 0.**
- Replay of all 187 October sentences whose first DeepSeek attempt was rejected, through the real
  `Translator` with mock engines returning the recorded raw outputs: 28 move from Groq to
  `first_route` rescue, 9 repetitive ones move from Groq to DeepSeek `api`, 0 published sentences
  become lost. (Other rows differ from history only because earlier guard/rescue changes postdate
  them.)
- Analyzer note: `scripts/analyze_runtime_events.py` and `utils/forensics_analysis.py` views that
  count only `result_source == "api"` will show roughly 28 fewer lines a month; those lines are
  under `guard_rescue`.
- Tests: first-route rescue does not call the fallback; ineligible first route still falls back and
  can be rescued last; faithful repetition accepted; looping output for an ordinary source or far
  longer than a repetitive source still rejected; fallback state untouched by a first-route rescue.
  Full suite: 1735 passed, 1 skipped.
- Live check: `translation.guard_rescue.selected.first_route` per hour, and spot-check those lines.

## 5. Not done

A provisional candidate rejected for the same reason is still not shown early, and the final then
repeats the DeepSeek call (~0.7 s) before the first-route rescue applies.

## 6. Review log

Codex is no longer available (subscription cancelled 2026-10-12); reviews are done by a separate
read-only Claude subagent with fresh context.

### Post-implementation review (Claude subagent, 2026-10-12): PASS, no blockers

Verified: first-route rescue uses the same hook and eligibility as the last resort and the finalizer
still re-adjudicates; fallback state is untouched on a content rejection, also when the active route
is not route 0; the last-resort path is unchanged apart from `stage` and the metric; only the live
translator passes `output_rescue` (probe and provisional paths do not).

Non-blocking notes and follow-up:
- The repetition exemption counted punctuation and raw lengths, so `와!!!!!!!!!! 대박!!!!!!!!!!` or
  `아니 아니 아니 아니` → `不`×16 passed as faithful. Fixed: letters only, cap 1.25×; negative tests
  added. Replay re-run: 9 (was 10) repetitive sentences move to DeepSeek, 0 newly lost.
- Added a test that a first-route rescue leaves `FallbackState` alone (active route 0 and 1).
- Accepted, not changed: if the finalizer were to reject a candidate the hook accepted, the line is
  lost without asking the fallback. Both use the same adjudicator and source, and the replay of 187
  sentences showed no such case.
