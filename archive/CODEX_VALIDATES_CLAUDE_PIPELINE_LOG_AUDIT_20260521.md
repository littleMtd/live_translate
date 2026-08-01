# Codex Validation of Claude Pipeline LOG Audit — 2026-05-21

## A. Validation method
- Files checked:
  - `CLAUDE_PIPELINE_LOG_QUALITY_AUDIT_20260521.md`
  - `CODEX_PIPELINE_LOG_QUALITY_AUDIT_20260521.md`
  - `modules/translator.py`
  - `modules/translation_policy.py`
  - `utils/text_heuristics.py`
  - `data/default_slang.json`
  - `data/streamer_profiles.json`
  - `data/translation_profiles.json`
  - relevant `tests/test_translator.py`, `tests/test_translation_policy.py`, `tests/test_config.py`
- Logs checked:
  - Primary validation logs: `logs/runtime_events_20260519.jsonl`, `logs/runtime_events_20260520.jsonl`
  - Sanity checked: `logs/runtime_events_20260521.jsonl` was already known to be mock-only and not used as production evidence.
- Grep/search terms used:
  - HADES variants: `채나`, `찬나`, `챗나`, `챗나룡`, `챗나룸`, `챗나롱`, `채나롱`, `채나로`, `채나룬`, `젠나룽`, `츤나`, `짭봄주`
  - Isegye/MW:MEU terms: `릴파`, `릴라`, `릴랑`, `莉帕`, `莉朗`, `莉拉`, `고세구`, `주르르`, `리츠`, `이비`, `수아`, `초은`, `지안`, `지한`
  - Missed Codex terms: `SUBJU API`, `마크 영상`, `金Bongjun`, `성태님한테도`, `응원과 사랑`
  - Pipeline terms: `cache_status`, `result_source`, `retry_reason`, `timeout`, `meta_garbage_output`, `incomplete`, `stt_template_garbage`
- Tests or commands run:
  - No unit test suite was run; this was a read-only validation.
  - Ran `git status --short --branch`, `git diff --stat`, `git diff --cached --stat`.
  - Ran `Select-String`/`git grep` searches and small Python log parsers to count/source-check runtime events.
  - Ran read-only Python calls into current translator/policy helpers to verify that current code leaves `채나`, `찬나`, `챗나룡`, `챗나룸`, and stacked `성태님한테도` uncorrected, while canonical `챈나` is corrected.
- Staged state / dirty state summary:
  - Staged state clean.
  - Pre-existing dirty tracked file: `config.py`, local profile switch `mwmeu` -> `hades_chxxnnx`.
  - Pre-existing untracked audit/scratch files remain. This validation adds only this markdown document.

## B. Claude issue-by-issue validation

### Claude issue ID/title: C1 — STT mishear `채나` bypasses HADES name canonicalization
- Verdict: confirmed.
- Evidence:
  - Runtime evidence exists in `logs/runtime_events_20260520.jsonl:1633`, `run_id=20260520T141444Z-37304`: source contains `천사채나`, target contains `天使-chan`.
  - Additional HADES runtime examples exist in `logs/runtime_events_20260519.jsonl:462`, `:1581`, and `logs/runtime_events_20260520.jsonl:415`, `:417`.
  - Current code still has only `("챈나",)` as the Chxxnnx source alias, and `_SOURCE_NORM_BY_PROFILE["hades_chxxnnx"]` only contains `服주 -> 섭주`.
  - Current helper verification: canonical `챈나야` triggers correction to `Chxxnnx`; `채나야`, `찬나야`, `챗나룡`, and `챗나룸` do not.
- Agreement / disagreement:
  - Agreement: Claude is correct that this survived Task #13/#14/#15/#16 and `服주->섭주` at the code level. None of those shipped changes adds `채나` or `챗나` source coverage.
  - Scope correction: a boundary-guarded `채나` alias may miss the strongest post-Task-15 evidence, `천사채나`, because it is joined to a Hangul prefix. A raw profile-gated normalization is more likely to catch the observed case, but it has more false-positive risk.
- Risk notes:
  - False-positive risk is real but bounded if HADES-profile-gated.
  - `채나` as a substring in viewer names or unrelated text could be rewritten if implemented as simple replacement.
- Whether it should enter merged backlog: yes.
- Suggested priority: P1.

### Claude issue ID/title: C2 — `isegye_lilpa` lacks post-processing name rules
- Verdict: confirmed.
- Evidence:
  - `logs/runtime_events_20260520.jsonl:582`: source `릴파님 생일 축하합니다!`, target `莉帕娘娘，生日快樂！`.
  - `logs/runtime_events_20260520.jsonl:592`: source contains `릴파`, target contains `莉帕`.
  - `logs/runtime_events_20260520.jsonl:671` and `:683`: source contains `릴파`/`릴랑`, target contains `莉帕`/`莉朗`.
  - `data/translation_profiles.json` declares `릴파 -> Lilpa`, `고세구 -> Gosegu`, `주르르 -> Jururu`, but current `_NAME_RENDERING_RULES` has no `isegye_lilpa` profile-specific entries.
- Agreement / disagreement:
  - Agreement: engine/prompt-only enforcement is insufficient for this run.
  - Disagreement on priority only: this is a strong P1/P2 candidate, but it should not outrank C1 if the next task is HADES-focused.
- Risk notes:
  - Low false-positive risk if profile-gated and limited to observed Chinese renderings.
  - Need avoid adding unconfirmed names such as `비챤` unless runtime evidence and canonical rendering are clear.
- Whether it should enter merged backlog: yes.
- Suggested priority: P1, second after HADES C1 for the current focus.

### Claude issue ID/title: C3 — `mwmeu` profile has high untranslated-Korean leak rate
- Verdict: partially confirmed.
- Evidence:
  - `logs/runtime_events_20260520.jsonl:946`: `이비`, `초은` leak in target.
  - `logs/runtime_events_20260520.jsonl:949`, `:952`, `:954`, `:963`: `리츠` leaks.
  - `logs/runtime_events_20260520.jsonl:966`: `이비姐姐` leaks.
  - `logs/runtime_events_20260520.jsonl:986`, `:989`, `:994`, `:996`: `초은` leaks.
  - `data/translation_profiles.json` has MW:MEU examples but no fixed `X -> canonical` glossary header; `_NAME_RENDERING_RULES` has no `mwmeu` rules.
- Agreement / disagreement:
  - Agreement: the runtime leak pattern is real.
  - Partial disagreement: the exact 17.7% rate was not independently recomputed here as a merged-audit gating fact. The qualitative issue is clearly supported.
  - This is blocked by canonical romanization choices, so it is not as ready as C1 or C2.
- Risk notes:
  - Medium risk due official-name spelling uncertainty (`리츠`, `이비`, `지안/지한`, etc.).
  - Needs user confirmation or official source lookup before implementation.
- Whether it should enter merged backlog: yes, but blocked.
- Suggested priority: P2 until canonical names are confirmed.

### Claude issue ID/title: C4 — Pre-Task-16 `-chan` leak when source contains canonical `챈나`
- Verdict: stale/already fixed by code, needs runtime validation.
- Evidence:
  - Historical log evidence exists at `logs/runtime_events_20260520.jsonl:300`: canonical `챈나야` source yielded `-chan`.
  - Current code includes `챈나` in Chxxnnx wrong forms and includes `rule.canonical` in `_replace_wrong_name_forms` alternatives.
  - Current helper verification changes canonical `챈나야` + `-chan` to `Chxxnnx`.
- Agreement / disagreement:
  - Agreement: no new fix should be opened from this issue alone.
  - Agreement with Claude's caveat: there is no post-Task-16/`服주` production runtime log validating it.
- Risk notes:
  - Low risk; runtime validation only.
- Whether it should enter merged backlog: no as a fix; yes as a runtime validation check.
- Suggested priority: P3 validation.

### Claude issue ID/title: C5 — Viewer/donor names and ad-hoc terms leak as Hangul
- Verdict: partially confirmed / out of scope for immediate fix.
- Evidence:
  - `logs/runtime_events_20260520.jsonl:301` leaks `짭봄주`.
  - `logs/runtime_events_20260520.jsonl:443` leaks `손바람님`, `아이쑥싹쑥싹`, and related noisy names/terms.
  - `logs/runtime_events_20260520.jsonl:69` leaks `젠나룽`.
- Agreement / disagreement:
  - Agreement: the leaks are real.
  - Agreement: most are not safe to fix without user confirmation.
  - Disagreement: some terms overlap with C1 server-name variants and should be triaged separately from generic viewer/donor names.
- Risk notes:
  - High false-positive and wrong-identity risk.
- Whether it should enter merged backlog: only as a candidate glossary collection task, not implementation.
- Suggested priority: P3/defer.

### Claude issue ID/title: C6 — `구독과 좋아요는 저에게 큰 힘이 됩니다` mixed with real content
- Verdict: partially confirmed, subsumed.
- Evidence:
  - `logs/runtime_events_20260520.jsonl:549` has mixed template plus `릴라님`; target starts after template stripping and leaves `莉拉님`.
  - Current policy rejects both no-`아주` and `아주` variants as isolated templates.
  - `utils/text_heuristics.py` includes both `구독과 좋아요는 저에게 큰 힘이 됩니다` and `구독과 좋아요는 저에게 아주 큰 힘이 됩니다`.
- Agreement / disagreement:
  - Agreement: this should not be a separate next task if framed only around `구독과 좋아요`.
  - Additional Codex note: Claude did not account for the distinct `시청자 여러분의 응원과 사랑은 ... 큰 힘이 됩니다` variant, which current policy does not reject.
- Risk notes:
  - Template stripping must not block real thank-you speech.
- Whether it should enter merged backlog: no as C6; yes separately for the `응원과 사랑` variant if runtime recurrence matters.
- Suggested priority: Not separate; Codex missed-issue task P2/P3.

### Claude issue ID/title: C7 — Global `_SOURCE_AWARE_TARGET_REPLACEMENTS` overlap for `히나`
- Verdict: structurally confirmed, runtime impact not reproduced.
- Evidence:
  - `modules/translator.py` has global source-aware replacements for `히나`, `끼윤`, `예난`, `철구`.
  - No inspected runtime sample proves a current bad user-visible output from this structure.
- Agreement / disagreement:
  - Agreement: profile scoping is cleaner.
  - Disagreement: this is not a LOG-first quality issue yet; it should not compete with C1/C2 or Codex X2/X3.
- Risk notes:
  - Refactor risk is low but not justified as an immediate quality task without runtime impact.
- Whether it should enter merged backlog: optional cleanup only.
- Suggested priority: P3.

### Claude issue ID/title: C8 — Engine timeouts at around 13s p99, retries absorbed
- Verdict: confirmed observation, not the right next task.
- Evidence:
  - Independent parse of `logs/runtime_events_20260520.jsonl`: 722 non-mock translation events; 11 events had `retry_count > 0`, all `retry_reason=timeout`, all ended `status=success`.
  - Same log also has 5 failed non-mock translation events, but those are not the same as Claude's absorbed timeout-retry set.
- Agreement / disagreement:
  - Agreement: do not tune timeout/retry knobs based only on the 11 absorbed retries.
  - Disagreement by omission: dropped failed translations remain a separate issue from the absorbed-retry observation.
- Risk notes:
  - Latency/cost regressions are likely if retry knobs are changed casually.
- Whether it should enter merged backlog: no as timeout tuning; yes as separate failure logging/fallback task if dropped subtitles are prioritized.
- Suggested priority: Not worth doing as stated.

## C. Validation of Claude top recommended next task
Task:
Add `채나` STT-mishear normalization or alias under HADES profile.

Answer:
- Supported by runtime evidence? yes.
  - Strongest current-like sample: `logs/runtime_events_20260520.jsonl:1633`, `천사채나` -> `天使-chan`.
  - Additional samples: `logs/runtime_events_20260519.jsonl:462`, `:1581`; `logs/runtime_events_20260520.jsonl:415`, `:417`.
  - `챗나룡` and `챗나룸` are also backed by runtime evidence:
    - `logs/runtime_events_20260519.jsonl:384` and `:1483` for `챗나룡`
    - `logs/runtime_events_20260519.jsonl:1492` for `챗나룸`
    - `logs/runtime_events_20260520.jsonl:229` for `챗나롱`
- Current after shipped tasks? yes at code level, with runtime caveat.
  - No post-Task-16/`服주->섭주` production log exists, but current code still does not cover `채나`, `찬나`, `챗나룡`, or `챗나룸`.
  - The shipped tasks fixed canonical `챈나` handling and `服주`, not these misheard source forms.
- Correct layer? source normalization first; name rule second if needed.
  - For `채나`/`찬나`: profile-gated source-side normalization is preferable to only extending `_NameRenderingRule`, because it feeds the engine/cache canonical source and catches outputs before the engine reinforces the wrong form.
  - Adding only `_NameRenderingRule.source_aliases` is weaker and may not catch `천사채나` under the current boundary matcher.
  - For `챗나룡`/`챗나룸`/server-name variants: source normalization alone may not be enough unless the expected canonical target form is defined. These may need explicit source aliases plus target wrong forms or a separate server-name normalization policy.
- Recommended variants to include:
  - Include now: `채나` as the core recurring HADES mishear, with tests for `채나야`, `채나님`, and `천사채나`.
  - Consider including now if acceptable under the same task: `찬나`, because it has repeated runtime evidence and behaves like a close Chxxnnx mishear.
  - Include as evidence-backed but possibly separate sub-scope: `챗나룡`, `챗나룸`, `챗나롱`, `채나롱`, `채나룬`, `채나로`. These are real log terms but likely refer to a server/room name, not always the person name alone.
- Variants to exclude or defer:
  - Defer `젠나룽`: one runtime hit and unclear identity/canonical form.
  - Defer `짭봄주`: two runtime hits, plausible but unconfirmed identity; do not map to `Kim Bongjun` without user confirmation.
  - Defer broad fuzzy forms and viewer/donor names such as `손바람`, `키아`, `세나`.
- Required profile gating:
  - Required. This must be HADES-profile-gated only.
  - Non-HADES profile test must prove `채나` remains unchanged or at least does not trigger Chxxnnx canonicalization.
- Files likely touched:
  - `modules/translator.py`
  - `tests/test_translator.py`
  - Possibly profile data only if the implementation chooses a data-driven alias list.
- Required tests:
  - HADES `채나야` with target `-chan` -> `Chxxnnx`
  - HADES `천사채나` with target `天使-chan` -> `天使Chxxnnx` or agreed equivalent
  - HADES `찬나야` if included
  - HADES `챗나룡`/`챗나룸` only if canonical server/room form is agreed
  - Non-HADES `채나야` remains unchanged
  - Negative case for unrelated Hangul-containing words, if a boundary-aware implementation is attempted
- False-positive risks:
  - `채나` could be a viewer nickname or substring in an unrelated coined term.
  - Simple substring normalization catches `천사채나` but is broader than current source alias boundaries.
  - Server variants may conflate person name and server/room name.
- Final verdict on this as next task:
  - Yes, proceed as the merged next task if scope is **HADES-only and starts with `채나`/maybe `찬나`**.
  - Treat `챗나룡`/`챗나룸` as real evidence, but either define their canonical server-name rendering in the task or defer them to a second narrow follow-up. Do not blanket-normalize every `챗나`/`채나`-family token without tests.

## D. Issues Codex found that Claude missed

### Codex issue ID/title: X2 — Mid-sentence glossary terms are not deterministically enforced (`마크`, `SUBJU`)
- Whether still recommended: yes.
- Whether it should outrank Claude's top task: no for HADES-name focus; it should be near the top of the merged backlog.
- Why:
  - `logs/runtime_events_20260519.jsonl:1403` renders `마크 영상` as `Mark`.
  - `logs/runtime_events_20260519.jsonl:1320` leaves `SUBJU API` as `SUBJU API`.
  - Current repo has exact glossary entries but exact slang lookup does not apply inside longer sentences.
  - Claude marked `마크`/`SUBJU` as historical successes, but the mid-sentence failure mode remains current in code.

### Codex issue ID/title: X3 — Target correction can create `金Kim Bongjun`
- Whether still recommended: yes.
- Whether it should outrank Claude's top task: no, but it should be bundled with near-term name-rule hardening.
- Why:
  - `logs/runtime_events_20260519.jsonl:1457` has source `봉준, 김봉준` and target `봉준、金Bongjun`.
  - Current helper verification changes `金Bongjun` to `金Kim Bongjun`, which is a post-processing artifact.
  - Claude discussed generic viewer/ad-hoc leaks but did not isolate this deterministic target-side regression.

### Codex issue ID/title: X4 — Stacked suffixes block `성태님한테도` correction
- Whether still recommended: yes.
- Whether it should outrank Claude's top task: no.
- Why:
  - `logs/runtime_events_20260519.jsonl:1465` and `:1665` leave `Sungtae哥`.
  - Current `_source_alias_matches_at` reads `님한테도` as one suffix and rejects it even though the individual suffixes are known.
  - This is a focused translator/source-aware correction bug.

### Codex issue ID/title: X5 — Failed translations with `target_text=null`
- Whether still recommended: yes, after name/glossary work.
- Whether it should outrank Claude's top task: no.
- Why:
  - `logs/runtime_events_20260520.jsonl` has 5 non-mock failed translation events; `logs/runtime_events_20260519.jsonl:1606` drops a valid `마크 영상` sentence after about 20.7s.
  - Claude's C8 correctly says absorbed retries are not the next task, but that does not cover failed/null outputs.

### Codex issue ID/title: E1 — `응원과 사랑` STT template variant is not filtered
- Whether still recommended: yes, as a small policy task if it recurs in fresh logs.
- Whether it should outrank Claude's top task: no.
- Why:
  - `logs/runtime_events_20260519.jsonl:1348`, `:1352`, `:1355` show `시청자 여러분의 응원과 사랑은 ... 큰 힘이 됩니다` translated as real content.
  - Current policy rejects `구독과 좋아요...` and `시청해주셔서 감사합니다`, but not this phrase.
  - This is upstream STT hallucination plus policy coverage, not translator name handling.

## E. Disagreements requiring user decision

### issue: Whether to include server-name variants in the immediate HADES normalization task
- Claude position: do not add `채나로`/`채나롱`/`채나룬` server-name variants in T1; keep T1 to person-name `채나`.
- Codex position: `챗나룡`/`챗나룸`/`챗나롱`/`채나롱` are real runtime failures, but they need an explicit canonical server/room rendering before implementation. They can be included only if the task defines that rendering.
- evidence: `runtime_events_20260519.jsonl:384`, `:1483`, `:1492`; `runtime_events_20260520.jsonl:221`, `:229`, `:300`, `:301`, `:417`.
- recommended user decision: choose whether the server/room entity should render as `Chxxnnx龍`, `Chxxnnx room`, `Chxxnnx server`, a Korean-preserved name, or something else. Without that, ship only `채나`/possibly `찬나`.

### issue: How aggressive `채나 -> 챈나` normalization should be
- Claude position: prefer source normalization but with word-boundary guard.
- Codex position: source normalization is correct, but a strict boundary guard may miss `천사채나`, the best post-Task-15 evidence. A profile-gated substring replacement may be acceptable if tests document the risk.
- evidence: `logs/runtime_events_20260520.jsonl:1633` source `천사채나` -> target `天使-chan`.
- recommended user decision: accept broader HADES-only substring normalization for `채나`, or require a narrower rule and knowingly leave prefixed compounds for later.

### issue: Whether C2 `isegye_lilpa` should outrank HADES C1
- Claude position: C1 first, C2 second.
- Codex position: agrees for HADES/current-profile focus. If ranking globally by low-risk/high-confidence fixes, C2 is competitive because it is profile-scoped and low-risk.
- evidence: C1 affects current HADES config and recent HADES logs; C2 has many clean `릴파 -> 莉帕/莉朗/莉拉` examples.
- recommended user decision: proceed C1 first if HADES is the priority; otherwise C2 is a valid alternate first implementation task.

## F. Suggested merged top tasks

### 1. HADES-only `채나` source normalization / canonicalization
- Narrow scope: `채나` core mishear, optionally `찬나`; profile-gated to `hades_chxxnnx`.
- Testable: yes, with `채나야`, `채나님`, `천사채나`, and non-HADES no-op tests.
- Evidence-backed: yes.
- Not stale: current code still lacks this coverage.

### 2. Decide and then handle HADES server/room variants
- Narrow scope: only observed variants `챗나룡`, `챗나룸`, `챗나롱`, `채나롱`, `채나로`, `채나룬`.
- Testable: yes, once canonical target rendering is chosen.
- Evidence-backed: yes.
- Not stale: current code does not cover them.

### 3. Add `isegye_lilpa` profile-scoped name rendering rules
- Narrow scope: `릴파 -> Lilpa` first; add `고세구`/`주르르` only with observed wrong forms and tests.
- Testable: yes.
- Evidence-backed: yes.
- Not stale: no current rule exists.

### 4. Add deterministic mid-sentence glossary enforcement for `SUBJU` and context-gated `마크`
- Narrow scope: `SUBJU API -> 服主 API`; `마크` only in Minecraft/video/server contexts.
- Testable: yes.
- Evidence-backed: yes.
- Not stale: exact glossary exists, mid-sentence enforcement does not.

### 5. Harden HADES source-aware target correction
- Narrow scope: compound-safe `Bongjun` replacement and stacked suffix handling for `성태님한테도`.
- Testable: yes.
- Evidence-backed: yes.
- Not stale: current helper verification reproduces both gaps.
