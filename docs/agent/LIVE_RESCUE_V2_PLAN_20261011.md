# Live: widen the copied-source-name rescue (v2) (2026-10-11)

Author: Claude Code. Status: implemented; Codex PASS.
Scope: `_guard_rescue_text_ok` and its constants in `modules/translator.py`; tests. The output
guard, fallback chain, rescue trigger (only after every route was rejected), eligibility
(`script_safety` Hangul failures only, both stages checked), chooser, finalize re-adjudication,
history/cache handling and telemetry are unchanged except the policy label.

## 1. Evidence (offline re-adjudication with current code, no API calls)

October live runs (`run_kind=live`): 102 sentences published nothing and kept rejected candidates.
With today's code, 9 would now be accepted outright and v1 rescue would publish 31 more (40 total);
most of these were lost before v1 shipped (2026-10-10). For the rest, the closest candidate failed:
run not a whole source token (17, e.g. `유천` from `유천이`), more than 2 Hangul runs (9), predicate
-like run (9), Hangul share > 30 % (8), verbatim copy (7), other guard reasons (8), Han < 50 % (4).

The owner rejected a manual per-stream name-review queue (T26) as not a real solution.

## 2. Change (`GUARD_RESCUE_POLICY = "copied_source_name_v2"`)

1. Up to 4 distinct Hangul runs (was 2).
2. A run qualifies if it is a whole source token (one trailing particle stripped, as before) **or**
   a ≥ 2-syllable piece of one whitespace-delimited source word **whose word is not itself
   predicate-like** (new; added after Codex round 1 so 먹었습 from 먹었습니다 and 학생 from 학생이에요
   do not qualify). One-syllable pieces and pieces spanning words still fail. The piece may sit
   anywhere in the word (prefix or not), and "one particle stripped" applies to the token branch only.
3. Unchanged: each run ≤ 8 syllables and not predicate-like; Hangul ≤ 30 % of the text; Han ≥ 50 %;
   not a verbatim copy; both `protection_restored` and corrected stages must pass.

A 40 % Hangul ceiling was considered and dropped: it adds 4 sentences and would reverse the
reviewed 2026-10-10 decision not to rescue Hangul-heavy candidates (class B).

## 3. Trade-off accepted by the owner (2026-10-11)

The rule cannot tell an unknown nickname from a common noun the model failed to translate; both are
"word + particle" in the source. So `사람이라는 멤버가 새로 들어왔어요` → `有個叫사람的成員加入了。`
is now published (flagged `guard_rescue`) instead of dropped. The owner chose this explicitly: one
Hangul word in an otherwise translated subtitle beats losing the subtitle. The guard itself is not
weakened — these candidates are still rejected as `unexpected_hangul` and only the last-resort
rescue publishes them. Pieces of predicate-like words stay excluded, so `제 이름은 학생이에요` →
`我的名字是학생。` is still not published. Of the two tests that pinned "common noun → no subtitle",
the 사람이라는 one now pins "guard rejects, rescue publishes"; the 학생이에요 one is unchanged.

## 4. Validation

- Real lost sentences (102): v1 publishes 40, v2 publishes 55 (+15; 56 before the predicate-word
  restriction). All 15 reviewed: 14 keep nicknames/proper nouns in Hangul (부가땅, 구플뱅, 비번뱅, 춘봉,
  빵둥님, 육식님, …); 1 is garbled STT carried into the output (`길이 개 개 페이셜`). No common-noun case
  appears in the October data.
- "Up to 4 distinct runs" bounds distinct runs, not total Hangul: four 8-syllable runs can pass while
  Hangul stays ≤ 30 % and Han ≥ 50 % (Codex round 1). Accepted as designed.
- Tests: new positives (word piece `유천`, repeated `징버기`, three runs) and negatives (1-syllable
  piece, cross-word piece, five runs, predicate-like piece); all v1 refusal cases still refused;
  policy label pinned to `copied_source_name_v2`; exactly four runs, mixed token (`abc뽀뿌123`) and a
  predicate-word piece (`먹었습`) covered. `사람이라는 멤버` → rescued (guard still rejects);
  `학생이에요` → still not published (predicate-like word). Full suite: 1704 passed, 1 skipped.
- Live check: count `result_source=guard_rescue` with `guard_rescue.policy == copied_source_name_v2`
  per hour (the analyzer groups by `result_source` only) and spot-check them.

## 5. Review log

### Codex review round 1 (2026-10-11): PASS

No blocker; other defenses unchanged. Non-blocking: substring also admitted pieces of predicate
words (`먹었습` from `먹었습니다`) and of mixed tokens; four distinct runs is not a total-Hangul cap;
tests should cover exactly four runs, mixed tokens and pin the literal policy; the analyzer does not
group by policy.

### Claude Code follow-up

Pieces now require a non-predicate-like source word (tests added); the other notes are recorded in
§2/§4. Re-count: +15 instead of +16 (the dropped one was 진김밥 inside a predicate clause).

### Codex re-review (2026-10-11): PASS

The token branch returns first, so nothing v1 accepted is newly refused (1,778 v1-accepted cases
re-checked); predicate-word pieces (`먹었습`, `학생`) are refused while `유천` still qualifies; edge
punctuation handling matches the token branch. No new blocker.

### Code review (Claude Code, 2026-10-11): APPROVE with one low-severity fix, applied

Probing showed the word-level predicate check (final-ending list) missed connective/adnominal verb
forms, so stems such as 예쁘 (예쁘게) and 좋아 (좋아하는) qualified as pieces. The piece branch now also
refuses source words ending (after one particle strip) in 게/하/던/기 (`_GUARD_RESCUE_PIECE_WORD_ENDINGS`);
whole tokens are unaffected (징부기 from 징부기가 still qualifies). Note: v1 already rescued the whole
token 좋아하 and v2 keeps that. Tests added; docstring updated. Re-count unchanged: +15 (55 of 102).
Full suite: 1704 passed, 1 skipped.
