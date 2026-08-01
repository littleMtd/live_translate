# Claude Validation of Codex Pipeline LOG Audit — 2026-05-21

> Local validation document — **never push, never stage**, same class as `OPTIMIZATION_*.md` and the two parallel `CLAUDE_*_AUDIT_*.md` / `CODEX_*_AUDIT_*.md` discovery docs.
> Cross-checks Codex’s `CODEX_PIPELINE_LOG_QUALITY_AUDIT_20260521.md` against runtime logs and current `main` repo state. Does not implement, does not modify any source/test file.

---

## 1. Validation method

- **Files checked**
  - Codex’s document: `CODEX_PIPELINE_LOG_QUALITY_AUDIT_20260521.md` (line-by-line read).
  - My own audit: `CLAUDE_PIPELINE_LOG_QUALITY_AUDIT_20260521.md` (for cross-reference, no edits).
  - Repo source:
    - [`modules/translator.py`](modules/translator.py) (full read — `_NAME_RENDERING_RULES`, `_SOURCE_NORM_BY_PROFILE`, `_SOURCE_AWARE_TARGET_REPLACEMENTS`, `_source_has_name_alias`, `_source_alias_matches_at`, `_replace_wrong_name_forms`, `_KOREAN_NAME_SUFFIXES`).
    - [`modules/translation_policy.py`](modules/translation_policy.py) (slang lookup, template-strip, template-garbage logic).
    - [`modules/pipeline_events.py`](modules/pipeline_events.py) (how `profile_id` propagates).
    - [`modules/sentence_buffer.py`](modules/sentence_buffer.py) (force-cut + `incomplete=True` source).
    - [`data/default_slang.json`](data/default_slang.json) (exact-match slang dict, 75 entries).
    - [`data/streamer_profiles.json`](data/streamer_profiles.json) (STT terms / aliases).
    - [`data/translation_profiles.json`](data/translation_profiles.json) (per-profile prompt glossary; `standard.*` and `qwen.*`).
- **Logs checked**: `logs/runtime_events_2026051[6-9].jsonl` and `logs/runtime_events_2026052[0-1].jsonl` (6 days). Counted 4,052 real production translation events (nvidia engine + non-empty `profile_id`), filtered into per-day variant frequency tables. 5/21 log discarded (mock-engine only).
- **Grep / search terms used** (verifying Codex’s sensitive-term claims): `마크` `Minecraft` `Mark` `섭주` `섭쥬` `썹주` `SUBJU` `服주` `服主` `챈나` `채나` `챗나` `챗나룡` `챗나룸` `챗마` `채나야` `채나님` `채나로` `채나롱` `채나룬` `천사채나` `젠나룽` `츤나` `찬나` `봉준` `김봉준` `Bongjun` `Kim Bongjun` `金Bongjun` `성태` `성태님한테도` `성태님은` `성태형` `Sungtae` `KimSungtae` `고세구` `Gosegu` `高世久` `릴파` `Lilpa` `莉帕`; pipeline keywords `vad` `stt` `whisper` `groq` `cache` `retry` `timeout` `incomplete`.
- **Tests or commands run**:
  - Python JSONL extractor (read-only) under `.pytest-tmp/_audit_extract.py` to compute per-day variant frequency and to confirm Codex’s cited file/line entries match the contents.
  - `git log --pretty=format:"%h %ai %s" -25` for shipped-task timestamp boundaries (Task #13 = 5/19 15:32 UTC, Task #14 = 5/19 17:08 UTC, Task #15 = 5/20 06:58 UTC, Task #16 = 5/20 16:51 UTC, `11dabc8` HADES `服주→섭주` = 5/21 14:17 UTC).
- **Staged state / dirty state summary**: `git status --short` — only `config.py` modified (local `streamer_profile` swap, unrelated). 11 untracked files including my audit, Codex’s audit, this validation, optimization docs, scratch dirs. No staged changes. No new commits since `11dabc8`.

---

## 2. Codex issue-by-issue validation

### Codex X1 — “HADES Chxxnnx variants from STT are not normalized”

- **Verdict**: **partially confirmed**. The root claim is correct; the specific variant list is partly stale.
- **Evidence**:
  - All four lines Codex cites (`5/19:1483/1492/1577/1581`, `5/20:443`) exist exactly as described and the source/target text matches.
  - Per-day variant frequency I re-derived (real production runs only):
    - `챗나룡`: 5/16:1, 5/18:3, 5/19:2, **5/20:0**
    - `챗나룸`: **5/19:2 only**
    - `챗마`: 5/16:1, 5/17:1, 5/19:3, **5/20:0**
    - `채나`: 5/16:4, 5/17:1, 5/18:5, 5/19:6, **5/20:13** (rising)
    - `채나야`/`채나님`/`채나로`/`채나롱`/`채나룬`: appear on 5/20
    - `천사채나`: 5/20:1 — only **post-Task-15** instance still showing `-chan` leak (post-Task-15 hades run `20260520T141444Z-37304`, my Issue C1).
    - `젠나룽`: 5/20:1
  - The STT mishear *pattern* has shifted from `챗_` (extra ㅅ batchim) toward `채_` (missing ㄴ batchim) over the week. By 5/20 the dominant variant family is `채나-*`, not `챗나-*`.
- **Agreement / disagreement**:
  - **Agree** the bug is current: `채나` STT mishear bypasses the HADES rule because [`modules/translator.py:163`](modules/translator.py#L163) `source_aliases = ("챈나",)` and `_SOURCE_NORM_BY_PROFILE[hades_chxxnnx]` (lines 110–114) lacks 채나 entries. Confirmed post-Task-15 by my Issue C1 evidence (`천사채나` → `天使-chan`).
  - **Disagree on variant list**: Codex’s explicit named list `챗나룡, 챗나룸, 채나야, 채나님` is *partly stale*. `챗나룡` / `챗나룸` last appeared 5/19 and are absent from 5/20. The post-shipped current variants are predominantly `채나` / `채나롱` / `채나로` / `채나야` / `채나님` / `채나룬` / `천사채나`. Including `챗나룡` / `챗나룸` in a normalization map is acceptable as low-cost defense-in-depth, but their inclusion should be labeled “historical, may not re-occur” rather than “recently observed”.
- **Risk notes**:
  - Adding `채나 → 챈나` to source normalization (or `채나` to `source_aliases`) is the highest-ROI change.
  - False-positive guard required: `채나` is two common Hangul syllables; a word-boundary guard analogous to `_source_alias_matches_at` should be reused.
  - Profile gating mandatory — non-HADES profiles must be unaffected.
- **Whether it should enter merged backlog**: **yes**, with refined variant list.
- **Suggested priority**: **P1**.

### Codex X2 — “Mid-sentence glossary terms are not deterministically enforced (`마크` and `SUBJU`)”

- **Verdict**: **stale / mostly already fixed**. Code-level structural concern is real but not currently visible in post-shipped logs.
- **Evidence**:
  - Both Codex citations (lines `1320` and `1403` in `5/19`) are at UTC `12:32:17` and `12:38:06` — Task #13 was committed at `15:32 UTC` 5/19. Both citations are **PRE-Task #13**.
  - In post-Task-13 5/20 logs (722 real translations), `마크` appears in source **once** and renders as `Minecraft` correctly (`땡귤이 마크 프로게이머라 …` → `tinggyul是Minecraft職業選手…`). `SUBJU` (all-caps) does **not** appear in any 5/20 source. `섭주` in source twice → `服主` both times.
  - The structural claim — that `policy.slang_result` uses `_slang.get(text)` (exact-match only) and so `마크 영상` mid-sentence does not hit slang — is verifiably true ([`modules/translation_policy.py:139-140`](modules/translation_policy.py#L139)). But it is not currently *visible* because the prompt+glossary path is enforcing `Minecraft` in real outputs.
- **Agreement / disagreement**:
  - **Disagree on priority**. Codex marks P1; current evidence is too thin (1 success / 0 failures in 5/20) to justify P1.
  - **Disagree on scope risk**. A new “deterministic mid-sentence glossary enforcement” layer (Codex’s recommended fix) is a substantial structural change. Building it before confirming the current prompt-level enforcement is breaking sounds like premature optimization.
- **Risk notes**: deterministic mid-sentence enforcement risks false positives like `마크` (Mark, person) → `Minecraft` outside game context. Codex acknowledges this; the fix is non-trivial.
- **Whether it should enter merged backlog**: **defer**. Re-evaluate if a post-`11dabc8` runtime shows fresh `마크 → Mark` or `SUBJU → SUBJU` mistranslations.
- **Suggested priority**: **P2 / Hypothesis** (move to “needs runtime samples” bucket).

### Codex X3 — “Source-aware target correction can create mixed-script artifacts such as `金Kim Bongjun`”

- **Verdict**: **confirmed as a code-level current risk, not yet visible in runtime**.
- **Evidence**:
  - Codex’s cited line `5/19:1457` (UTC 12:42, **PRE Task #13**) actual output is `봉준、金Bongjun、林敏教、Tayo、民結、歐梅基姆、高世久、朱魯魯爾、一帕、吉姆布克？`. The output already contains `金Bongjun`, but NOT yet `金Kim Bongjun` — because at that time, Task #15 (which added 봉준 as a wrong_form) had not shipped.
  - Manual regex trace against **current** `_NameRenderingRule(_HADES_PROFILE_ID, ("김봉준","봉준"), ("김봉준","봉준","Bongjun","奉俊","奉主"), "Kim Bongjun")`:
    - Pattern alternatives sorted by length DESC: `Kim Bongjun(11)`, `Bongjun(7)`, `김봉준(3)`, `奉俊(2)`, `奉主(2)`, `봉준(2)`.
    - Input `봉준、金Bongjun`:
      1. `봉준` at index 0 matches → `Kim Bongjun、金Bongjun`
      2. `Bongjun` at index 16 (inside `金Bongjun`) matches → `Kim Bongjun、金Kim Bongjun`
    - Result: `金Kim Bongjun` artifact, confirming Codex’s claim.
  - Codex’s phrasing “current source-aware correction fixes some parts but would turn `金Bongjun` into `金Kim Bongjun`” is technically accurate.
- **Agreement / disagreement**:
  - **Agree** the compound-script regression is structurally present in current code.
  - **Disagree on priority urgency**: not yet observed in any post-Task-15 run. In 5/20, the only 봉준 source (`솜주먹, 봉준, 백인석` in run 044657, pre-Task-15) produced raw Hangul `봉준` in target — the rule didn’t even fire at that time. We have no post-Task-15 봉준 sample to confirm the regression *manifests*.
  - **Risk of fix**: a “compound-safe” regex change is fiddly — adding a left-boundary guard could break cases where source-aware correction needs to rewrite Hangul `봉준` that follows a non-Hangul prefix (legitimate).
- **Risk notes**:
  - Defensive tests are easy to add without code change first. If a regression test reproducing `金Kim Bongjun` from a simulated `金Bongjun` engine output is added, that alone would document the risk and create regression coverage.
- **Whether it should enter merged backlog**: **yes**, but as a **defensive code-level cleanup, not a runtime-fix-driven task**.
- **Suggested priority**: **P2**.

### Codex X4 — “Stacked Korean suffixes block source-aware name correction for `성태님한테도`”

- **Verdict**: **confirmed as a current code-level bug** (structurally true), with strong but pre-Task-14 runtime evidence.
- **Evidence**:
  - Codex’s cited lines `5/19:1465` and `5/19:1665` both exist and show the symptom. Timestamp `5/19 12:42 UTC` and `15:44 UTC` are **PRE Task #14** (5/19 17:08 UTC commit).
  - Same source `성태님한테도 연락해보려고…` recurs across **5/18 08:55, 5/19 12:42, 5/19 15:44** (3 occurrences). Targets contain `Sungtae哥` / `Sungtae老師` / raw `성태님` — never canonical `KimSungtae`.
  - 5/20 logs: no `성태` source at all (streamer didn’t discuss Sungtae), so the post-Task-14 runtime behavior is unobserved.
  - Manual trace of `_source_alias_matches_at(source="성태님한테도", alias="성태", start=0)`:
    - `end = 2`, all of `source[2:6] = "님한테도"` are Hangul, so `suffix_end` advances to 6.
    - `suffix = "님한테도"` (whole compound suffix).
    - `"님한테도" in _KOREAN_NAME_SUFFIXES` → **False** (set has only individual particles: `님`, `한테`, `도`, etc.).
    - Returns False → rule does **NOT** trigger.
  - The bug is structurally present in current code on `main`.
- **Agreement / disagreement**:
  - **Agree** with the diagnosis and the fix idea (split suffix scan into greedy multi-particle decomposition, OR add a small set of observed compound suffixes).
  - **Disagree on “priority P1”** absent post-Task-14 evidence. The phrase `성태님한테도` recurred 3× in 4 days of pre-Task-14 logs but never in post-Task-14 logs (because the streamer’s topic shifted). The structural bug is current; the runtime visibility is not.
- **Risk notes**:
  - Greedy decomposition is the safer fix path: walk forward consuming particles from `_KOREAN_NAME_SUFFIXES` until you hit a non-particle Hangul or end of string. Negative-guard test: `가성태님` (where `가성태` is a different name) must NOT match `성태` (start>0 with Hangul-syllable preceding — already handled at line 216–217).
- **Whether it should enter merged backlog**: **yes**.
- **Suggested priority**: **P2** (downgrade from Codex P1 due to lack of post-Task-14 evidence).

### Codex X5 — “Valid Korean sources sometimes produce empty failed translation output”

- **Verdict**: **confirmed current but low-priority engine-reliability issue**.
- **Evidence**:
  - Codex cites `5/19:1606` (`오 좋겠다. 마크 영상도 봐줘. …` → `target=null status=failed engine=nvidia latency_ms=20734`). UTC 15:40 = post-Task-13, pre-Task-14. Real engine timeout.
  - 5/20 had 5 such failures total: 2 stellive (`TMI TMI`, `짠 오늘은 …`), 3 mwmeu (`그래서 뭔가 학교인데도`, `그리고 제가 또 전에는 일본 …`, `근데 처음에 한국에서 들어간지 …`). All 5 had `result_source=none`, `engine=nvidia`. 4 of 5 were marked `incomplete=true`.
  - In total: 5 failures / 722 real translations = **0.7%**. 11 successful retries / 722 = 1.5%. The fallback/retry path absorbed 11 timeouts that did NOT become failures, so the in-flight fallback is largely working.
  - This is the same phenomenon as **my Issue C8** (engine timeouts).
- **Agreement / disagreement**:
  - **Agree** the symptoms exist.
  - **Disagree on priority P1**. 0.7% empty-failed rate, mostly on incomplete fragments. The proposed fix (provider failure-reason logging + targeted retry on null + incomplete-fragment policy) is *scope-broad* relative to the visible impact.
  - **Note**: incomplete-fragment failures are not quality bugs — `incomplete=true` source like `그래서 뭔가 학교인데도` is a dependent clause; an empty output is actually preferable to a hallucinated standalone subtitle.
- **Risk notes**: changing fallback/retry behavior risks doubling latency on already-slow streams. Cost implications.
- **Whether it should enter merged backlog**: **as logging/observability**, yes. **As a runtime-quality fix**, no — defer until failure rate climbs.
- **Suggested priority**: **P2 / observability-only**.

### Codex E1 — Upstream STT template `시청자 여러분의 응원과 사랑은…큰 힘이 됩니다`

- **Verdict**: **partially confirmed**.
- **Evidence**:
  - All cited lines exist. In 5/20 logs the pure-template variants are correctly filtered (`filter_reason=stt_template_garbage`); only mixed-real-content variants leak through to engine, where the template head is then strip-removed by `strip_stt_template_fragments`.
  - My **Issue C6** classifies this as “sub-symptom of C2” — the visible problem is the trailing `님` suffix retention on names (e.g. `릴라님 → 莉拉님`), not the template itself.
- **Agreement / disagreement**:
  - **Agree** the family of phrases warrants logging improvement and continued template-list coverage.
  - **Disagree on need for a new task**. The current `STT_TEMPLATE_*` infrastructure is catching the pure variants; the mixed-content cases are *not* a template-detection failure but a name-rendering failure.
- **Whether it should enter merged backlog**: **defer** unless fresh variants appear.
- **Suggested priority**: **P3**.

### Codex E2 — Upstream STT variants `챗나룡 / 챗나룸 / 채나 / 츤나`

- **Verdict**: **confirmed as upstream STT** (matches my Issue U1).
- **Evidence**: per-day frequency table in §1 confirms the upstream phonetic mishear is widespread; my §2 X1 validation also covers this.
- **Agreement / disagreement**: agree completely.
- **Whether it should enter merged backlog**: **yes**, as defense-in-depth via translator-side normalization (the X1 task).
- **Suggested priority**: handled within X1.

### Codex E3 — Upstream incomplete fragment failures

- **Verdict**: **confirmed but acceptable**. Matches my Issue U4.
- **Whether it should enter merged backlog**: as **logging improvement** (add `forced=True` cut reason and VAD chunk id to runtime event), not as a behavior fix.
- **Suggested priority**: **P3 / observability**.

### Codex F1/F2/F3 — Historical/stale findings

- **F1** (canonical `챈나` → `-chan` leak): **agree fixed** by Tasks #15+#16. Post-Task-15 hades run `20260520T141444Z-37304` shows 5 source `챈나` events all rendered as `Chxxnnx` ✓.
- **F2** (`服주→섭주` shipped in `11dabc8`): **agree stale** (no runtime evidence; same as my Hy2).
- **F3** (meta/Q&A output in `5/16` logs): **agree stale**; current `_looks_like_meta_garbage_output` filter catches the leftover cases (e.g. `글랜스` filtered in 5/20).

### Codex G1/G2/G3 — Hypotheses

- **G1** (stale cache): **agree** — same as my Hy5. Cache hit rate is structural to live streams.
- **G2** (emit/order latency perception): **agree** — no captured display transcript to confirm.
- **G3** (VAD logging cross-stage IDs): **agree** — would help debugging. Maps to my U4.

---

## 3. Validation of Codex top recommended next task

**Task as stated by Codex**: *“Add narrow HADES source normalization for repeated Chxxnnx STT variants: 챗나룡, 챗나룸, 채나야, 채나님.”*

- **Supported by runtime evidence?** **Partial**.
  - `채나야` / `채나님`: supported across 5/16–5/20 (incl. one post-Task-15 instance `천사채나` 5/20).
  - `챗나룡` / `챗나룸`: supported on 5/16–5/19 only; **not seen in 5/20**. Defensible as historical, but no longer dominant.
  - Stronger 5/20 variants Codex didn’t name: bare `채나` (13 hits), `채나로` (2), `채나롱` (3), `채나룬` (1), `천사채나` (1, post-Task-15).
- **Current after shipped tasks?** **yes**, for `채나-family`. The shipped Tasks #13/#14/#15/#16 + `11dabc8` do **not** address `채나` (no ㄴ) or its clitics — none of them widened `source_aliases` or `_SOURCE_NORM_BY_PROFILE` for these forms.
- **Correct layer?** **source normalization** (preferred) over `_NameRenderingRule` alias extension. Reasons:
  1. Source normalization (`_SOURCE_NORM_BY_PROFILE`) runs BEFORE slang lookup, cache lookup, and engine. Mapping `채나 → 챈나` at source means:
     - cache reuse: a re-utterance with `채나` in the same session hits the same cache entry as one with `챈나`.
     - few-shot adherence: the engine sees canonical `챈나` per its profile prompt, which already says `챈나 → Chxxnnx`.
     - downstream `_NameRenderingRule` for `챈나` then takes care of any residual `-chan` / canonical-leak — no second rule needed.
  2. Alias extension on `_NameRenderingRule` runs ONLY at post-processing — engine still sees `채나`, slang/cache miss on the canonical key, post-fix may still arrive too late if engine produced a Chinese transliteration (no Chinese wrong_form for `채나` was ever in the rule).
- **Recommended variants to include** (post-shipped runtime evidence, 5/20-weighted):
  - `채나` (P1 — highest current frequency)
  - `채나로`, `채나롱`, `채나룬`, `채나야`, `채나님`, `천사채나` (clitic-bearing canonical-mishear forms)
  - `채나의` (defensive — not seen but symmetric)
- **Variants to exclude or defer**:
  - `챗나룡` / `챗나룸`: include with comment "historical 5/16–5/19; defensive low-cost coverage" OR defer if minimal-surface-area is preferred.
  - `챗나롱`: one 5/20 hit; borderline.
  - `젠나룽` / `츤나` / `찬나` / `챗마`: too rare and too phonetically distant from `챈나` — risk of false-positive higher. Defer to hypothesis bucket.
  - **Do NOT** absorb server-name variants (`채나로` / `채나롱` / `채나룬` distinct from the person name) into the *person* `_NameRenderingRule`. They may need a separate server-name canonical. Prior audit (`OPTIMIZATION_QUALITY_AUDIT_20260519.md` §4B item 4) flagged this.
- **Required profile gating**: **mandatory HADES-only**. Use `_SOURCE_NORM_BY_PROFILE[_HADES_PROFILE_ID]` (existing pattern from `11dabc8` `服주→섭주`).
- **Files likely touched**:
  - `modules/translator.py`: append entries to `_SOURCE_NORM_BY_PROFILE[_HADES_PROFILE_ID]` map; optionally add a small `_NameRenderingRule.wrong_forms` extension to absorb Chinese transliterations that may emerge (e.g. `채纳` if it appears).
  - `tests/test_translator.py`: positive cases per variant; profile-gated negative cases; word-boundary negative case (e.g. `채나무` if real Korean; only an example).
  - Optionally `data/streamer_profiles.json` HADES `stt_terms` to ADD `채나` as an STT bias term (low risk, may help Whisper learn the canonical).
- **Required tests** (minimum):
  - HADES profile + source `채나야 고마워` → target contains `Chxxnnx`, no `-chan`/`채나`.
  - HADES profile + source `천사채나` → `天使Chxxnnx`.
  - HADES profile + source `채나로` → either canonicalized server form (if added) or `Chxxnnx로`-like form (depending on scope).
  - Non-HADES profile + source `채나야 고마워` → unchanged (no normalization applied).
  - Negative-guard: any false-positive candidate the team can think of (e.g. unrelated Korean word containing `채나` as a substring — verify none in `_KOREAN_NAME_SUFFIXES` context exists).
- **False-positive risks**:
  - `채나` is two syllables that could conceivably appear in unrelated words. The prior audit (§6) accepted this trade-off but recommended profile gating. **Profile gating is sufficient mitigation** because non-HADES streams won’t apply the normalization.
  - The bigger risk is over-eager `채나로` / `채나롱` rewriting if the speaker uses these as server-name distinct from the person.
- **Final verdict on this as next task**:
  - **Supports being the next task**: high — runtime-current, narrow, code-localized, profile-gated, low blast radius, evidence-backed.
  - **Refinement vs Codex**: include `채나-family` (5/20-frequent), demote `챗나룡 / 챗나룸` from “key examples” to “defensive optional coverage”, explicitly call out server-name vs person-name canonical ambiguity for `채나로`/`채나롱`/`채나룬`.

---

## 4. Issues Claude found that Codex missed

### Claude C2 — `isegye_lilpa` profile lacks `_NameRenderingRule` entries

- **Whether still recommended**: **yes**.
- **Whether it should outrank Codex’s top task**: **no, second priority**.
- **Why**: `릴파 → 莉帕 / 莉朗 / 莉拉` is a *6-of-6 failure* in run `20260520T053821Z-136712` (isegye_lilpa, 92 events, 15.2% Hangul leak rate). The profile prompt declares `릴파 -> Lilpa` but the engine ignores it and produces Chinese transliterations. Same pattern as why HADES needed `_NameRenderingRule`. Codex’s audit lists isegye_lilpa under inspected runs but never surfaces this as an issue.

### Claude C3 — `mwmeu` profile lacks `[Fixed proper-noun glossary]` header → 17.7% Hangul leak

- **Whether still recommended**: **yes**, but **blocked on user romanization decisions** for member names (지한 / 이비 / 수아 / 리츠 / 초은).
- **Whether it should outrank Codex’s top task**: **no**. mwmeu has the highest Hangul leak rate but the user-decision blocker pushes it to T4 in my ranking.
- **Why**: mwmeu prompt in `data/translation_profiles.json` begins with `【MW:MEU 특화 범례】` only — no `[Fixed proper-noun glossary]\n- X -> Y` header. Every other profile has the header. The profile names appear repeatedly in 5/20 mwmeu runs (~43 distinct Hangul leaks across runs 101803 and 140152).

### Claude C7 — `히나` lives in global `_SOURCE_AWARE_TARGET_REPLACEMENTS`, not profile-scoped

- **Whether still recommended**: **yes**, as part of T2 (isegye_lilpa rules) or T5 (refactor).
- **Whether it should outrank Codex’s top task**: **no**.
- **Why**: code-consistency issue; not a visible runtime regression. The current global `_SOURCE_AWARE_TARGET_REPLACEMENTS` mixes profile-specific names (`히나`, `끼윤`, `예난`, `철구`) with truly-global mistranslation patches (`마가 뜨`, `붕 뜨`, `개복치`). Should be cleaned up.

---

## 5. Disagreements requiring user decision

Only listing real disagreements where user judgment is required:

### Disagreement D1 — Should `챗나룡 / 챗나룸` be included in the next-task source normalization map?

- **Issue**: Codex lists them as a “key example”; Claude (this validation) marks them as historical (last occurrence 5/19, none on 5/20).
- **Claude position**: include with low priority as defensive coverage OR defer entirely; emphasize `채나-family` (5/20-dominant) as the lead.
- **Codex position**: include as primary examples (potentially at parity with `채나야` / `채나님`).
- **Evidence**: per-day variant table in §1 (5/16–5/19 only for `챗나룡`; 5/20 absent). The streamer’s topic shifted between 5/19 and 5/20.
- **Recommended user decision**:
  - **Option A** (minimal surface): include only `채나` + clitic variants; add `챗나룡` / `챗나룸` only if a new runtime sample re-introduces them. Lower false-positive risk.
  - **Option B** (defense in depth): include both families now; one extra commit avoids future regression at the cost of a slightly larger normalization map. Acceptable if test coverage is good.

### Disagreement D2 — Should `_NameRenderingRule` aliases be extended OR `_SOURCE_NORM_BY_PROFILE` be extended?

- **Issue**: Codex’s X1 fix idea is non-specific (“profile-gated normalization or alias coverage”). The two approaches behave differently.
- **Claude position**: prefer `_SOURCE_NORM_BY_PROFILE` (source-side, runs before cache+engine+slang). See §3 reasoning.
- **Codex position**: ambiguous — could be either.
- **Evidence**: code trace in [`modules/translator.py:434`](modules/translator.py#L434) shows `_normalize_source_before_matching` runs **before** slang lookup and before cache lookup, while `_NameRenderingRule` post-processing runs only on the returned target. Cache reuse + few-shot adherence are both improved by source-side normalization; post-processing only catches Chinese mistranslations that already happened.
- **Recommended user decision**: confirm **source-side** as the layer. Then file ranking for T1 is settled.

### Disagreement D3 — Is Codex’s X2 (mid-sentence glossary enforcement for `마크` / `SUBJU`) worth doing as T2?

- **Issue**: Codex marks P1; Claude marks stale/defer (no post-Task-13 visible failure).
- **Claude position**: defer to hypothesis bucket. Re-evaluate after post-`11dabc8` runtime.
- **Codex position**: P1, start with `SUBJU` and `마크` in clear game/server/video contexts.
- **Evidence**: in 5/20 (post-Task-13), `마크` source → `Minecraft` target (1/1 correct). No `SUBJU` source samples in 5/20.
- **Recommended user decision**: **defer**. If the user disagrees, ship a smaller observability change (log mid-sentence glossary near-misses) before building a deterministic enforcement layer.

### Disagreement D4 — Is Codex’s X3 (compound-safe regex for `金Kim Bongjun`) worth doing as T3?

- **Issue**: Codex marks P1; Claude marks P2 (current code-level risk, no post-Task-15 visible artifact yet).
- **Claude position**: lower priority than T1/T2 because the regression hasn’t been observed in a real post-Task-15 run.
- **Codex position**: P1.
- **Evidence**: manual regex trace confirms `金Kim Bongjun` would arise IF engine produces `金Bongjun` with current code; no 5/20 post-Task-15 봉준 sample exists to confirm the artifact.
- **Recommended user decision**: **agree on P2**, defer until either (a) a post-Task-15 봉준 sample reproduces the artifact, or (b) the team wants defensive regression tests.

---

## 6. Suggested merged top tasks

Ranked, each narrow + testable + evidence-backed + not stale.

### Merged T1 — Profile-gated HADES source normalization for `채나-family` (and optional `챗나-family` defensive coverage)

- **Scope**: extend `_SOURCE_NORM_BY_PROFILE[_HADES_PROFILE_ID]` with a small map:
  - lead: `채나 → 챈나` (with word-boundary guard).
  - clitic-bearing variants: `채나야 → 챈나야`, `채나님 → 챈나님`, `채나로 → 챈나로`, `채나롱 → 챈나롱`, `채나룬 → 챈나룬`, `천사채나 → 천사챈나`.
  - optional defensive: `챗나룡 → 챈나룡`, `챗나룸 → 챈나룸`, `챗나롱 → 챈나롱` (user decision D1).
- **Non-goals**: no broad fuzzy matching, no Chinese-target wrong_forms (those are already covered by Task #15/#16 for `챈나`), no person-vs-server-name canonical decisions for the `~로/~롱/~룬` variants (server-name vs person-name ambiguity).
- **Files**: `modules/translator.py` (≤15 lines in `_SOURCE_NORM_BY_PROFILE`), `tests/test_translator.py`.
- **Tests**: positive per variant, profile-gating negative, word-boundary negative.
- **Risk**: medium (false-positive on `채나`); profile-gating already mitigates.
- **Why next**: highest current frequency residual hades issue; clear evidence path; narrow scope; small blast radius.

### Merged T2 — Add `_NameRenderingRule` entries for `isegye_lilpa` (Claude C2)

- **Scope**: introduce `_ISEGYE_LILPA_PROFILE_ID`; add rules for `릴파→Lilpa`, `주르르→Jururu`, `고세구→Gosegu` (already shared, may move into isegye scope), `비챤→VTuber-name-TBD`. Wrong_forms drawn from observed Chinese mistranslations `莉帕/莉朗/莉拉/朱魯魯/朱魯魯爾/高世久/一帕/一派`.
- **Non-goals**: don’t change profile prompt JSON (already declares the mapping), don’t introduce stellive/mwmeu rules in this task.
- **Files**: `modules/translator.py`, `tests/test_translator.py`.
- **Tests**: positive per name, profile-gating negative.
- **Risk**: low.
- **Why**: 15.2% Hangul leak rate in run `20260520T053821Z-136712`; same pattern as HADES post-Task-14; tight & well-bounded.

### Merged T3 — Capture one post-Task-16 + post-`11dabc8` HADES live run; runtime-validate Hy1/Hy2/T1

- **Scope**: collect a fresh HADES live session into `logs/runtime_events_YYYYMMDD.jsonl`. Diff observed targets containing `Chxxnnx`/`-chan`/`채나`/`服주` vs expectations. Write local-only `OPTIMIZATION_TASK16_RUNTIME_VALIDATION_*.md`.
- **Non-goals**: no code change. No plan modifications.
- **Files**: none (run app + write a review doc).
- **Tests**: N/A.
- **Risk**: none (observation).
- **Why**: Task #15/#16/`11dabc8` are all unverified at runtime; without this we keep flagging Hy1/Hy2 in every audit.

### Merged T4 — Codex X4: stacked-suffix support for `_source_alias_matches_at` (성태님한테도 etc.)

- **Scope**: in `_source_alias_matches_at`, replace the “whole-following-Hangul-block as one suffix” check with a greedy walk that consumes one particle at a time from `_KOREAN_NAME_SUFFIXES`. Boundary-test on the final non-Hangul character or end of source.
- **Non-goals**: no expansion of the alias list itself; no new names.
- **Files**: `modules/translator.py` (≤20 lines), `tests/test_translator.py`.
- **Tests**: `성태님한테도`, `성태님은`, `성태형한테`, negative `가성태님` (must not match).
- **Risk**: medium — must preserve existing false-positive protections (Hangul-syllable left boundary, non-Hangul right boundary).
- **Why**: structurally current bug confirmed by code trace; pre-Task-14 runtime evidence is consistent; recurrence likely whenever streamer discusses Sungtae again.

### Merged T5 — Codex X3 defensive: regression test for `金Bongjun → 金Kim Bongjun` compound-safety

- **Scope**: add **only a regression test** (no code change) that simulates engine output `봉준、金Bongjun` for a HADES source containing `봉준` and asserts the result is **not** `金Kim Bongjun`. If the test fails (which the regex trace says it should), promote to a code fix in a follow-up task with a left-boundary guard in `_replace_wrong_name_forms`.
- **Non-goals**: no code change yet; **discovery-driven regression test first** so the fix is justified by failing-test evidence.
- **Files**: `tests/test_translator.py` only.
- **Tests**: as scoped.
- **Risk**: very low.
- **Why**: code-level concern is real, but adding a fix without a failing test risks over-engineering; adding only the test lets the next session decide whether the fix is worth the regex-complexity tradeoff.

> **Out of merged top-5** (Codex’s X2 / X5 / E1 / E3 / G1–G3): deferred to hypothesis bucket per §2 verdicts.

---

> **Status**: Claude validation, 2026-05-21. Local-only, **not staged, not committed, not pushed**. The two parallel discovery docs (`CLAUDE_PIPELINE_LOG_QUALITY_AUDIT_20260521.md` and `CODEX_PIPELINE_LOG_QUALITY_AUDIT_20260521.md`) and this validation all stay local. Next concrete user-facing step: choose between **(a) start T1 plan** (refined HADES `채나` normalization), **(b) start T2 plan** (isegye_lilpa name rules), or **(c) T3 runtime capture** before any code work.
