# Live: stop losing whole subtitles to copied source Hangul (2026-10-10)

Author: Claude Code. Status: proposal for cross review. Scope: the live publication guard in
`modules/translator.py` (guard evaluation ~L1140–1400, `_translation_output_guard` L1408, `_finalize_translation_result` L2563). No provider, prompt,
STT or audio change.

## 1. Evidence (read-only analysis of live runtime events, run_kind=live)

Runs 2026-10-04 .. 2026-10-08 (8 live runs, 1461 sentences): **57 sentences (3.9 %) published no subtitle**;
run `20261008T154934Z-37252` lost 30 of 368 (8.2 %). None were API errors: every lost sentence had DeepSeek
`finish_reason=stop` replies that the output guard rejected (`attempts[].status=rejected_output`, 107 rejected
candidates; 107 counted below). Classification of the rejected candidates (`output_guard.candidate_output` vs `source_text`):

| Class | n | Example (source ⇒ rejected candidate) |
|---|---|---|
| A: every Hangul span is a verbatim source substring and ≤ 25 % of the target's non-space chars | 64 | `뭐 이 정도? 야, 뽀뿌야!` ⇒ `什麼？就這程度？喂，뽀뿌！` |
| B: all Hangul verbatim from the source but > 25 % | 21 | `…야, 뽀뿌야!` ⇒ `什麼這樣？ 嘿，뽀뿌야！`; `양재사님 고마워요.` ⇒ unchanged copy |
| X: `unactivated_entity_target` (a canonical from another profile) | 13 | `챈대장의 숨찰 시간.` ⇒ `Chaenna隊長喘口氣的時間。` (active profile isegye_lilpa) |
| C: Hangul not present in the source | 6 | `버거님의 …` ⇒ `…징버거님…`, `온 챠니` ⇒ `온챠니` |
| other | 3 | repetitive / numbers-only / empty |

Context: the user's standing decision is that unknown nicknames stay in Hangul (option B). The honorific allowance
(`_source_honorific_name_terms`, L618–693) already publishes `랑코님`-style names for the same reason; vocatives
(`뽀뿌야`), bare nicknames and short phrases are not covered, so the whole subtitle is lost.

## 2. Design: a last-resort publication, not a looser guard

Normal per-attempt guard decisions are unchanged. Only when **every** attempt for a sentence was rejected does a
new finalizer step consider the rejected candidates:

- **L1 copied-Hangul rescue.** Eligible candidate: its only failed invariants are `unexpected_hangul` /
  `raw_unexpected_hangul` (owner `script_safety`); every Hangul span in the corrected candidate is a verbatim
  substring of the prepared source; CJK (Han) characters are ≥ 50 % of its non-space, non-punctuation characters;
  and it is not identical to the source after whitespace normalisation. Among eligible candidates pick the lowest
  Hangul ratio (ties: earliest attempt). Publish it with `result_source="guard_rescue"`,
  `quality_flags += ["published_with_source_hangul"]`.
- **L2 cross-profile name repair.** For `unactivated_entity_target` with no other failed invariant: replace each
  unactivated canonical in the candidate with the source alias that produced it (the registry alias found verbatim
  in the source; longest match). If the repaired text then passes the normal guard evaluation (re-run, not
  bypassed), publish it as `result_source="guard_rescue"` with flag `cross_profile_name_restored`.
- Never rescued: Class C (Hangul not in source), meta/refusal, simplified Chinese, Japanese, missing canonical
  obligations, request-protection/escrow failures, repetitive output, empty output.
- The rescue runs after the fallback chain, so no extra API request is ever made for it.

## 3. Validation

- **Offline replay (no API calls)**: feed each logged rejected candidate (107) + source through the new finalizer
  decision. Expected: Class A rescued; Class B rescued only where Han ≥ 50 % and not a verbatim copy
  (`양재사님 고마워요.` stays rejected); X repaired to `챈대장의 …` / `…챈나`; C and "other" unchanged. Report the
  resulting lost-sentence rate per run.
- Unit tests: each eligibility rule and each never-rescue class; L2 alias selection and the re-run guard; runtime
  event fields (`result_source`, flags, the rescued attempt index) and that `subtitle_emitted` is true.
- Full pytest; existing benchmark/replay suites must not change accepted outputs (rescue only touches sentences that
  were previously lost).

## 4. Assumptions

- A1: DeepSeek candidates that only copy source Hangul are otherwise correct translations (spot-checked 20 of 64).
- A2: The registry can map an unactivated canonical back to the alias present in the source (true for 챈나→Chaenna).
- A3: Showing a subtitle with a Korean nickname is preferable to showing nothing (user statement, 2026-10-10).

## 5. Review log

### Codex plan review round 1 (2026-10-10): REVISE

Blockers: (1) no entry point — `_finalize_translation_result` only receives one `provider_result`; (2) L1's ratio
rule admits whole untranslated Korean clauses padded with Chinese; (3) checking only the corrected text lets a
`raw_unexpected_hangul` rejection through when a correction removed non-source Hangul; (4) L2 repair to the source
alias would still fail `unexpected_hangul` and corrections may re-render it; (5) no contract for publication,
success recording, cache/history and telemetry; (6) class counts sum to 107, not 104 (fixed: 107 rejected candidates),
and per-candidate replay cannot prove "only previously lost sentences change".

### Claude Code revision (round 2)

- **Scope**: L1 only. **L2 is deferred** (13 candidates; needs registry alias provenance and a policy for publishing
  a non-activated name — a separate plan).
- **R1 entry point** (`modules/translation_runtime.py:call_with_fallback`): immediately before
  `log.error("All engines failed")` (L554) — reached only when every route returned an unusable result — call a new
  optional `output_rescue(attempts, source)` hook with `local_attempts`. Not reached by: a selected success (L402/L542),
  provider exceptions (re-raised, L367/L481), cache hits and provisional promotion (both bypass `_call_with_fallback`,
  translator L2486–2495). The hook returns the index of one attempt or `None`. The chosen attempt keeps
  `status="rejected_output"` and its `output_guard`, and gains `rescue={"policy": "copied_source_name_v1",
  "index": i}` and `selected_for_output=True`; `finish(result_text_of_that_attempt, engine_index)` returns it, so
  `_last_fallback_details.selected_attempt` identifies the rescue.
- **R2 eligibility (names only, both stages)**. An attempt is eligible iff its `output_guard` is a dict with
  `disposition=="rejected"`, a non-empty `failed_invariants` whose every entry is `owner=="script_safety"` and
  `reason in {"unexpected_hangul","raw_unexpected_hangul"}`, and **both** `candidate_raw_output`
  (protection-restored raw stage) and `candidate_output` (corrected) satisfy:
  - every maximal Hangul run is a *source name token*: equal to a whitespace token of the prepared source after
    stripping edge punctuation, or to that token minus exactly one trailing particle from a fixed list
    (`야 아 이 가 은 는 을 를 의 도 랑 이랑 한테 에게 이다 다`);
  - at most **2** distinct Hangul runs, each 1–8 syllables;
  - Hangul syllables ≤ 30 % of non-space, non-punctuation characters and Han ≥ 50 %;
  - the text is not equal to the source after whitespace normalisation.
  Counter-examples that must stay rejected: `오늘 공연 취소됐어요，今天的直播節目安排如下。` (3 runs, clause),
  `양재사님 고마워요.` (verbatim copy), any run not in the source (e.g. `징버거님` when the source says `버거님`).
  Among eligible attempts choose the lowest Hangul ratio, ties → earliest attempt.
- **R3 finalize contract**: `_finalize_translation_result` gets `rescue: dict | None`. With a rescue it still runs
  `_adjudicate_translation_candidate(..., publication=True)`; it publishes only if the adjudication's full failure
  set is again limited to the two script_safety reasons **and** R2 holds for the adjudicated text (recomputed, not
  trusted from the hook). Otherwise it fails exactly as today. On success: `status="success"`,
  `result_source="guard_rescue"`, quality flag `published_with_source_hangul`, attempt index recorded;
  `_final_script_rejection_reason` (L1458, caller L4323) must not re-reject a `guard_rescue` outcome — it will be
  checked against the same R2 rule. Recording: `history.remember` + `write_history` (viewers saw it, keeps context)
  but **no** `cache_store` / `db_store` (a later identical sentence takes the normal path).
- **R4 telemetry**: runtime event keeps every attempt as logged today; adds `guard_rescue` (policy, attempt index,
  Hangul runs kept) and `subtitle_emitted` comes from the real emission path, not set directly. Metric
  `translation.guard_rescue.published`.
- **R5 validation**:
  - Rebuild the 57 lost sentences from the logs **with their full attempt lists in order** and run the hook +
    finalize eligibility; report a *historical-simulation* lost rate (not a live claim) and list **every** rescued
    output for manual review (the user sees the list).
  - Integration tests with fake engines through `call_with_fallback` + `_finalize_translation_result`: normal
    success unchanged; reject-then-success unchanged; all rejected & eligible → rescued; all rejected & ineligible →
    failed as today; raw-only `raw_unexpected_hangul` where the raw stage has a non-source run → not rescued;
    provisional promotion and cache hit paths never call the hook; rescued text not written to cache/DB;
    `_final_script_rejection_reason` does not drop it; provider exception path unchanged.
  - Full pytest and the existing translation benchmark/replay suites: no accepted output may change.

### Codex plan review round 2 (2026-10-10): REVISE

Resolved: B1 (entry point), B4 (L2 deferred), B6 (counts and replay unit). Remaining: B2 — a source token is not
proof of a name (`공연 취소됐어요，…` with two copied runs still passes); B3 — the raw stage must be
`candidate_stages.protection_restored.text` (what `raw_quality` checks), not the provider text; B5 —
`_outcome_used_api` (L1954) must treat `guard_rescue` as an API outcome and the attempt must be chosen with
`select_translation_attempt()`; new N1 — a terminal failure can mix rejected and empty attempts.

### User decisions (2026-10-10) and round-3 changes (Claude Code)

- **B2 (user accepts the residual risk with a filter)**: an occasional ordinary noun left in Hangul is acceptable
  when it saves the subtitle. Added filter: a Hangul run that ends in a predicate ending is never a name
  (`요 다 니다 까 죠 네 지 고 서 면 는데 어서 아서 었 았 했 됐 해 돼 세요 ㅂ니다`, after particle stripping), so copied
  clauses such as `취소됐어요` disqualify the candidate. Expected residual: ~3 of 26 historical rescues keep a common
  noun (e.g. `봉지`).
- **B3**: both `candidate_stages.protection_restored.text` and `candidate_output` must exist and satisfy R2; the
  finalizer re-check uses the adjudication's own two stages.
- **B5**: `_outcome_used_api` accepts `guard_rescue`; the hook marks the attempt with `select_translation_attempt()`.
- **N1 (user decision: rescue)**: trigger = the chain ended without a selected result and at least one attempt is
  R2-eligible; `empty` attempts do not block it; provider exceptions still re-raise as today (never reach the hook).

## 6. Implementation checks (Claude Code, 2026-10-10)

- Code: `translation_runtime.call_with_fallback(output_rescue=...)` (terminal path only, `select_translation_attempt`,
  `guard_rescue` on the attempt); `translator`: `_guard_rescue_text_ok` / `_guard_rescue_eligible` /
  `_choose_guard_rescue`, finalizer re-adjudication + re-check, `result_source="guard_rescue"`,
  `published_with_source_hangul`, `guard_rescue` event field, `_outcome_used_api`, `_final_script_rejection_reason`
  re-check, `_record_rescue` (history + transcript, no memory/DB cache).
- Tests: `tests/test_guard_rescue.py` (rule cases incl. the reviewer's two-run clause counter-example, raw-stage
  non-source run, missing stage; hook not called on success or provider exception; rejected+empty rescued; reject
  then clean fallback unchanged; ineligible fails; rescue never cached, history remembered; event/API/final-gate).
  Full pytest: see commit.
- **Historical simulation** (logged attempts through `_choose_guard_rescue`; not a live claim): 1461 sentences,
  lost 57 (3.9 %) → 33 (2.3 %); run 20261008T154934Z-37252 30/368 → 14/368. All 24 rescued outputs reviewed by
  hand: kept Hangul are names/nicknames/proper nouns (뽀뿌, 띵, 오아, 랑박사, 홍귀, 부가, 쯔양, 징알모신, …) plus the
  accepted common-noun residual (봉지, 물결표).

### Codex post-implementation review round 1: FIX → Claude Code fixes

- B1: predicate endings now exactly the round-3 list (incl. 니/해/돼) on every run and on the run minus one
  particle; the ≥3-syllable exception was removed (short names such as 민지 are simply not rescued).
- B2: the chooser compares the Hangul **ratio**; ties → earliest. The test oracle was wrong and now expects index 2.
- B3: source tokens strip edge punctuation only (`[^\w]`), so `abc민지123` is not `민지`.
- `translation.guard_rescue.published` is counted in `_record_rescue` (when the deferred success runs), not at
  finalize. Added tests: 말해/간다/먹고도/abc민지123, a forged hint rejected by publication adjudication, the
  transcript write. Simulation with the stricter rule: lost 57 (3.9 %) → 35 (2.4 %), 22 rescued (봉지/물결표 no
  longer qualify). Full pytest 1685 passed, 1 skipped.
