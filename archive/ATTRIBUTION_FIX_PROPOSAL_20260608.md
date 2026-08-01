# Proposal: Forced-Cut Source Attribution Review (Step 1)

> LOCAL ONLY — do not commit/push (proposal doc, same policy as OPTIMIZATION_*.md).
> Workflow step 1 of 7: Claude Code proposal → Codex cross-review → revision →
> re-review → implementation only after blockers resolved or user proceeds →
> post-implementation review. No code edited. No implementation. No commit.

## 1. Goal & problem statement

**Goal:** Determine whether, and how, to correct a *suspected attribution
over-count* in which a forced-prefix/forced-blob `translation` event can list
`source_utterance_ids` that belong to a prior sentence, and decide the correct
fix layer (runtime behavior vs labeling/data-model). A prior change (commit
`3b45025`) moved this code from dropping to carrying residual source ids; the
review should weigh both failure directions (dropping a true source vs claiming
a prior source) without assuming either prior or current behavior is correct.

**Problem statement (neutral):** During manual labeling, several forced-cut
subtitles were observed whose attributed prior source chunks, on playback,
contained the *previous* sentence's speech rather than the current text. The
working concern is that the runtime attribution channel
(`source_utterance_ids`) over-counts on forced residual cuts, which—if
confirmed—would make STT-vs-translation labels unreliable for affected samples.
This is **not** asserted to explain all STT errors; multi-speaker, clip audio,
BGM, and unclear-audio remain separate failure classes.

## 2. Claims (each tagged by evidence type)

| ID | Claim | Evidence | Source |
|----|-------|----------|--------|
| C1 | `SentenceBuffer.pop_ready()` forced-prefix residual branch reassigns `self._buffer = residual` and resets only `_first_token_time`; it does **not** call `reset()`, so `_source_utterance_ids` / `_chunk_count` / `_total_audio_seconds` / `_source_avg_logprobs` / `_source_no_speech_probs` persist into the next cut. | code | `modules/sentence_buffer.py`: residual carry at lines 175-176 (inside the `if residual` block opened at 162; the `else` reset is 177-179); `reset()` clears all tallies at lines 104-112; tally fields declared 98-102, appended in `push()` 121-124 |
| C2 | The carry-forward was introduced intentionally, replacing a prior zero-out that *dropped* residual source ids; the commit states the residual "may derive from any chunk seen so far and the sources cannot be cleanly partitioned." | code / audit | `git show 3b45025 -- modules/sentence_buffer.py` |
| C3 | `merge_cuts()` concatenates `source_utterance_ids` / `source_avg_logprobs` / `source_no_speech_probs` of both cuts; a carry-forward cut later merged would compound duplicated ids. Merging is gated by `_can_merge_cuts()` which caps total source count / text length. | code | `modules/sentence_splitter.py`: `merge_cuts()` lines 105-120 (id concat at 117-119); guardrail `_can_merge_cuts()` lines 40-54 (`max_merge_source_count` / `max_merge_text_chars`) |
| C4 | The translator suppresses duplicates by identical **output text** within `_DEDUP_SUBTITLE_SEC=5.0`, not by source ids; residual fragments with differing text (e.g. seq48 "최대 체력 증가." vs seq49 "최대 체력") are not suppressed. | code | `modules/translator.py`: `_DEDUP_SUBTITLE_SEC` at line 1055; dedup compares `result == last_result` around line 1180 |
| C5 | `sample_labeling_cases.py` reconstructs `source_chunks[]` / `source_chunk_usages[]` **from** the runtime event's `source_utterance_ids` and computes `prior_overlap` from prior translations' source ids; it does not invent chunks. So the sample faithfully mirrors whatever the runtime event recorded. | code | `scripts/sample_labeling_cases.py` `build_prior_source_usage()`, `_sample_entry()` source-id iteration |
| C6 | In run `20260606T095302Z-42260`: 412 `translation`, 195 forced cuts, 198 multi-chunk, 11 translations share ≥1 source id with the immediately-preceding translation. | runtime | provided population + repeated-id query on `logs/runtime_events_20260606.jsonl` |
| C7 | Consecutive examples seq 163/164, 393/394, 48/49 show the later event's `source_utterance_ids` containing the earlier event's ids. | runtime | `logs/runtime_events_20260606.jsonl` |
| C8 | On playback of affected cases, inherited prior chunks contain previous-sentence speech, sometimes whole chunks / several seconds, beyond normal VAD overlap; in some cases the primary source + translation are still acceptable despite prior-audio pollution. | audit | user listening notes |
| C9 | VAD emits overlapped chunks (`vad_overlap_sec`), but each overlapped chunk receives its own `utterance_id`; that audio-level overlap is distinct from the *same* `utterance_id` appearing in two sentences' source lists. | code | `modules/audio_capture.py` `_VadState._emit()` / `_next_overlap()`; id minting in `modules/stt.py` |
| C10 | The contamination magnitude appears to differ by regime (more forced cuts -> more reuse); a label-distribution split by cut type shows higher `b_stt_error` and lower `ok` on forced vs natural cuts. | runtime / audit | round-0 sample joined to `sentence` cut_reason. **HYPOTHESIS ONLY, NOT SIGN-OFF EVIDENCE: small N (34 natural / 26 forced) and confounded — forced cuts are also genuinely harder audio, so the b-rate gap cannot be attributed to the attribution issue. Must not be used to justify the fix.** |
| C11 | The 11 reuse cases in run `20260606T095302Z-42260` are not all forced: cut_reason breakdown is forced_blob 7, forced_prefix 3, **natural 1**. A natural-ending cut can therefore still carry inherited ids (likely an inheriting cut that ended naturally after a prior residual carry-forward, but the path is not yet confirmed). | runtime | repeated-id query, see Section 6 |
| A1 | A one-line "reset tallies after forced prefix" would re-introduce the pre-`3b45025` behavior of dropping residual-origin source ids. | assumption (to verify) | follows from C1+C2; verify by inspecting which test encodes the tradeoff |
| A2 | A meaningful share of "b_stt_error" labels on forced cuts is attributable to attribution pollution rather than genuine mishearing. | assumption (to verify/falsify) | see §5. **Confounded (forced = harder audio); cannot serve as sign-off evidence for the fix.** |

## 3. Scope

- Runtime source-attribution behavior on forced-prefix and forced-blob cuts in
  `modules/sentence_buffer.py`, and its propagation through
  `modules/sentence_splitter.py` (`emit_cut`, `merge_cuts`),
  `modules/pipeline_events.py` (`SentenceEvent`, `transcription_to_sentence`,
  `sentence_metadata`, `source_confidence_summary`), into the `translation`
  runtime event via `modules/translator.py`.
- Offline reconstruction in `scripts/sample_labeling_cases.py` insofar as it
  consumes the above.
- The data-model question of whether "current source" and "evidence source"
  should be separated.

## 4. Non-goals

- Not changing STT engine, model, or accuracy; not adding diarization,
  WhisperX, pyannote, or speaker-ID.
- Not claiming to fix multi-speaker, clip audio, BGM, or song/humming failure
  classes.
- Not addressing the forced-cut *rate* itself (why ~47% of cuts are forced) in
  this task, except to note it as upstream context.
- Not re-running or re-scoping the round-0/round-2 labeling conclusions.

## 5. Assumptions & how to verify/falsify

| ID | Assumption | Verify / falsify |
|----|------------|------------------|
| AS1 | The over-count reaches the **runtime `translation` event**, not only the offline sample/UI. | Trace one affected `seq` end-to-end: `SentenceCut.source_utterance_ids` → `transcription_to_sentence` → `sentence_metadata` → translator event fields. Confirm the id list in the raw `runtime_events_*.jsonl` line, independent of the sample tool. |
| AS2 | A1: resetting tallies reproduces the pre-3b45025 drop. | Apply the reset locally in a scratch test (not committed), run `test_sentence_buffer.py`; observe which assertions flip. |
| AS3 | A2: pollution inflates b_stt_error. | Re-label (or re-examine) affected samples distinguishing current-source vs evidence-source; compare b-rate on natural vs forced before/after a candidate fix. Falsified if affected samples are independently genuine mishearings. |
| AS4 | No downstream consumer depends on the carried ids as a feature. | Grep consumers of `source_utterance_ids` / `prior_overlap`. Note: `sample_labeling_cases.py` prior-overlap detection (C5) currently *relies on* seeing reused ids; a runtime fix would change what that detector reports. |
| AS5 | The residual text's true origin chunk(s) cannot be recovered from current state. | Inspect what state `SentenceBuffer` retains relating text to chunks (e.g. any text→chunk offset mapping vs a flat id list and concatenated buffer), and judge whether residual origin is recoverable. |

## 6. Proposed investigation steps (before any implementation)

1. **Confirm the propagation layer (AS1).** Establish whether raw `translation`
   events carry the over-counted ids, or whether they are introduced only in
   reconstruction. This determines whether the fix is runtime or labeling.
2. **Map the data dependency (AS4).** Enumerate every consumer of
   `source_utterance_ids` and of the `prior_overlap` signal; identify any that
   would change behavior under each candidate option — especially the labeling
   tool that detected this.
3. **Locate the tradeoff test (Q8).** Identify the existing test(s) that encode
   the carry-forward as desired behavior (candidate:
   `tests/test_sentence_buffer.py::test_forced_prefix_residual_keeps_source_attribution`)
   so the proposal can state explicitly what behavior change is intended.
4. **Characterize residual origin (AS5).** Determine whether the buffer can, even
   in principle, attribute the residual to specific chunk(s) without new state.
5. **Quantify at runtime level.** Re-derive the reuse rate directly from raw
   events (not the sample) and break down by `forced_prefix` vs `forced_blob` vs
   `merged:*` vs `natural` to size each affected path. Reproduction query (the
   source of C6 / C11):

   ```python
   import json
   rows = [json.loads(l) for l in open('logs/runtime_events_20260606.jsonl', encoding='utf-8') if l.strip()]
   RUN = '20260606T095302Z-42260'
   tr = [r for r in rows if r.get('event_type')=='translation' and r.get('run_id')==RUN and r.get('sequence_id') is not None]
   tr.sort(key=lambda r: r['sequence_id'])
   def ids(r): return set(r.get('source_utterance_ids') or [])
   reuse = [tr[i] for i in range(1, len(tr)) if ids(tr[i]) & ids(tr[i-1])]
   from collections import Counter
   # observed: total 412, reuse 11, cut_reason of reuse = {forced_blob:7, forced_prefix:3, natural:1}
   print(len(tr), len(reuse), Counter(r.get('cut_reason') for r in reuse))
   ```

   The single `natural` reuse case (C11) means the fix scope must NOT be assumed
   to be the forced branch only until step 1/4 confirm how that case inherited
   ids.

## 7. Candidate implementation approaches (evaluate; do not pre-select)

The prompt's Options A-D, restated with the underlying decision each implies.
The central decision is **whether `source_utterance_ids` means "chunks that
produced this sentence's text" (current-source) or "chunks whose audio may be
evidence for this sentence" (evidence-source)** — today it conflates both.

### Option A — Reset source tallies after forced-prefix residual
- Upside: residual-derived sentence no longer claims prior chunks as its audio.
- Downside: reproduces the pre-3b45025 behavior (A1/AS2); a residual from a
  straddling chunk would lose its origin id. Does not provide a way to attribute
  a residual that derives from a chunk already partly emitted.

### Option B — Carry residual text but tag carried ids as evidence-only / uncertain
- Upside: separates current-source from evidence-source; stops treating old
  chunks as current audio while preserving the straddle evidence.
- Downside: requires a data-model change beyond a flat `source_utterance_ids`
  (e.g. a parallel `evidence_source_utterance_ids` or per-id role); sample tool,
  tests, and any UI must consume the distinction. Largest surface.

### Option C — Keep runtime behavior, adjust labeling/sample interpretation only
- Upside: smallest live-behavior change; no risk to subtitle path.
- Downside: does not correct the raw `translation` event if AS1 holds; any other
  consumer of `source_utterance_ids` keeps the over-count. Pushes the
  distinction into the sample tool only.

### Option D — Duplicate source-span suppression at sentence/translator stage
- Upside: can suppress pure residual-duplicate events (seq48/49 type).
- Downside: addresses the duplicate-fragment symptom (C4), not the attribution
  semantics; risks suppressing legitimate short follow-ups unless criteria are
  precise. Orthogonal to A/B/C; could combine with B.

### Recommended direction & implementation contract

After Codex Step-2 confirmation that raw `translation` events carry the
over-count (RV2 supported) and that the residual carry-forward was deliberate
(RV4 supported), the options narrow:

- **Option A alone — excluded.** Resetting reproduces the pre-3b45025 drop;
  Codex confirmed 3b45025 was a deliberate evidence-preservation change, so A
  alone regresses it.
- **Option C alone — excluded.** Codex confirmed the over-count is in the raw
  runtime event, so a labeling-only change leaves every other consumer of
  `source_utterance_ids` wrong.
- **Option D — optional add-on** for the seq48/49 duplicate-fragment symptom;
  does not resolve attribution semantics on its own.
- **Option B — recommended primary direction**, because it is the only option
  that both stops treating prior chunks as current audio AND preserves the
  straddle evidence 3b45025 cared about.

**Implementation contract (Option B) — proposed, requires explicit user sign-off
before implementation because it is a data-model change:**

1. Split the attribution into two fields on `SentenceCut` / `SentenceEvent` and
   the `translation` event:
   - `source_utterance_ids` (CURRENT-source): chunks whose transcription text
     contributed to THIS sentence's emitted text.
   - `evidence_source_utterance_ids` (EVIDENCE-source): chunks carried as
     residual context whose text already went out in a prior sentence but whose
     audio may still be relevant evidence.
2. forced_prefix prefix cut: current-source = all accumulated ids (unchanged —
   the prefix text derives from them).
3. On residual carry-forward (`sentence_buffer.py` 162-176): move **all**
   accumulated ids into a pending EVIDENCE set; reset `_chunk_count` /
   `_total_audio_seconds` / current `_source_*` lists so the residual sentence's
   CURRENT tally counts only chunks arriving after the carry.
   **AS5 RESOLVED (user decision, §12): the boundary-straddling chunk is
   evidence-only.** This makes the rule "all carried -> evidence, post-carry
   new chunks -> current," which does NOT require identifying the straddling
   chunk and therefore does NOT depend on a text->chunk offset map (the D1
   limitation): it is implementable with the existing flat lists.
   Consequence to honor downstream: a residual sentence's own residual-text
   origin chunk lands in evidence-source, so the labeling listening protocol must
   treat evidence-source as "may include this sentence's residual-origin audio,"
   not purely "prior sentence."
4. `merge_cuts()`: concatenate current with current, evidence with evidence;
   never promote evidence into current.
5. Downstream: `sample_labeling_cases.py` joins audio/STT for the listening test
   on CURRENT-source; reports prior-overlap from EVIDENCE-source. The existing
   prior-overlap detector (which today infers overlap from id reuse) is
   re-expressed against the explicit evidence field.
6. Back-compat: events without the new field are treated as current-source-only
   (old data), so `schema_version` handling and older logs do not break.

**Fallback if user rejects the data-model change:** Option A + C minimal —
reset current tallies on carry-forward (stop the over-count) and document in the
labeling tool that residual-origin evidence is not recoverable. This accepts the
3b45025 regression knowingly. Listed only as the explicit alternative, not
recommended.

## 8. Test plan

Behavior the proposal intends to make testable (final assertions to be fixed
once the option is chosen):

- A forced-prefix residual case does not attribute the full prior source span as
  the *current-source* audio of the next sentence.
- Evidence/confidence is not silently dropped when residual text may derive from
  a straddling chunk (i.e. Option A's regression is detectable).
- `translation` events expose enough structure for labeling to separate
  current-source chunks from prior/evidence chunks (only if B is chosen).
- `sample_labeling_cases.py` derives prior-overlap from runtime evidence without
  inventing chunks, and its prior-overlap report remains correct under the new
  semantics (AS4).
- Natural cuts preserve existing attribution behavior (regression guard).
- `merge_cuts()` does not amplify carried/duplicated ids (C3).
- The existing test that encodes the old tradeoff is updated deliberately, with
  a comment naming the behavior change.

### Concrete assertions (stated against the recommended Option B in §7; adjust if a different option is chosen)

Given a forced_prefix cut on chunks `[A, B, C]` that carries a residual forward,
followed by a new chunk `D` producing the next sentence:

- `T1`. The next sentence's **current-source** (`source_utterance_ids`) must NOT
  contain any chunk accumulated before the carry-forward `[A, B, C]` (AS5: all
  carried, including the straddling chunk, are excluded from current). Asserted:
  `set(next.source_utterance_ids).isdisjoint({A, B, C})`.
- `T2`. The straddling chunk `C` is evidence-only for the residual sentence
  (AS5 resolved, §12): after carry-forward the residual sentence's current-source
  contains only chunks that arrived after the carry; `[A, B, C]` are all in
  evidence-source. Asserted: `set(residual.source_utterance_ids) == {D}` when one
  new chunk `D` arrived; `{A, B, C} <= set(residual.evidence_source_utterance_ids)`.
- `T3`. The carried chunks are NOT silently dropped: they appear in the new
  **evidence-source** field (`evidence_source_utterance_ids`), preserving the
  3b45025 concern that residual evidence not be lost. Asserted:
  `{A, B, C} <= set(next.evidence_source_utterance_ids)`.
- `T4`. `chunk_count` / `audio_seconds` on the residual sentence reflect only its
  current-source chunks, not the carried ones (no double-counting of audio).
- `T5`. A pure-residual cut with no new chunk (seq48/49 type) has **empty**
  current-source and full evidence-source. Asserted:
  `next.source_utterance_ids == ()` and
  `set(next.evidence_source_utterance_ids) == {A, B, C}`. Emission behavior:
  the subtitle is still emitted (no subtitle-behavior change in this task);
  empty current-source is the explicit signal of a pure-residual fragment.
  Suppressing such fragments is Option D, deferred as a separate
  subtitle-behavior decision (§12).
- `T6`. Natural cuts with no preceding carry-forward are unchanged
  (regression guard); a natural cut that DID follow a carry-forward (C11) obeys
  T1-T4.
- `T7`. `merge_cuts()` of two cuts does not move evidence-source ids into
  current-source, and respects `_can_merge_cuts()` caps.

Focused commands (to be refined by the chosen option):

```powershell
.\live-subtitle-env\Scripts\python.exe -m pytest tests\test_sentence_buffer.py tests\test_sentence_splitter.py tests\test_pipeline_events.py tests\test_labeling_review_server.py
.\live-subtitle-env\Scripts\python.exe -m pytest tests\test_integration.py tests\test_collection_sanity_report.py tests\test_sample_labeling_cases.py
```

## 9. Runtime validation plan (post-implementation)

Collect a replayable run with `LIVE_TRANSLATE_DUMP_AUDIO=1`, regenerate a sample,
and compare before/after:

- repeated source-id rate between consecutive translations (target: forced-cut
  reuse no longer attributes prior whole chunks as current-source);
- prior-overlap sample count and how the labeling tool now reports it;
- forced-cut count and per-path source-span behavior;
- label reliability for STT-vs-translation classification on a re-labeled subset
  (AS3) — distinguishing subtitle-quality from attribution-quality (C8);
- clean-speech STT quality unchanged (regression guard, not an improvement
  claim).

Do not claim improvement on multi-speaker / clip / BGM / song cases unless
validated separately.

## 10. Reviewer checklist (claims to validate — not confirmation questions)

For each, return: ✅ supported / ⚠️ partially supported / ❌ unsupported, with a
quoted code/runtime reference. Findings with no evidence cannot ground a
sign-off; forward-looking concerns may be listed as non-blocking hypotheses.

- **RV1 (C1, Q3):** How does the forced-prefix residual branch handle the
  source/audio/confidence tallies relative to `reset()`? Quote the lines.
- **RV2 (AS1, Q1):** Do raw `translation` events carry the over-counted ids, or
  is it only sample/UI reconstruction? Trace the path.
- **RV3 (C9, Q2):** Is the observed reuse explained by normal VAD overlap, or is
  it distinct (same `utterance_id` in two sentences vs overlapped-but-distinct
  ids)? What threshold separates acceptable overlap from over-attribution?
- **RV4 (C2, Q4):** Was the carry-forward intentional, and what evidence problem
  did it solve?
- **RV5 (A1, Q5):** Would a one-line reset after forced prefix be sufficient? If
  not, why, and which test encodes the prior tradeoff (Q8)?
- **RV6 (C3, Q6):** How does `merge_cuts()` combine the two cuts'
  `source_utterance_ids`, and what happens to carried/duplicated ids?
- **RV7 (C4, Option D):** Does the translator dedup by source id or only output
  text, and does that leave residual-duplicate fragments un-suppressed?
- **RV8 (C5, AS4):** Does the sample tool consume runtime ids faithfully, and
  which consumers (incl. its own prior-overlap detector) change behavior under
  each option?
- **RV9 (Q7, §7):** Which layer should change — runtime behavior, labeling
  attribution, or the current-source/evidence-source data model — and is any
  single option sufficient or is a composition required?
- **RV10 (C10/A2, AS3):** Is the claim that pollution inflates b_stt_error
  supported, partially supported (confounded by forced=harder audio), or
  unsupported by the available evidence?

---

Return cross-review findings against the claims above; do not implement.

---

## Codex Cross-Review (Step 2) — verbatim, DO NOT EDIT

Verdict: **REVISE** (direction not rejected; proposal not yet ready for
implementation).

Confirmed:
- Root-cause framing largely holds: forced-prefix residual branch retains
  source/audio/confidence tallies — `modules/sentence_buffer.py` (line 162).
- `3b45025` confirmed as an intentional tradeoff: avoids dropping residual source
  evidence but admits over-count.
- Raw runtime event itself carries the over-count, not sample-UI fabrication;
  spot-checked seq 48/49, 163/164, 393/394 lines in
  `logs/runtime_events_20260606.jsonl`.
- `scripts/sample_labeling_cases.py` (line 304) consumes runtime
  `source_utterance_ids` directly to build the sample — supported.
- `modules/sentence_splitter.py` (line 105) `merge_cuts()` concatenates source
  ids directly, can amplify carried attribution.

Required revisions:
1. Fix unverifiable mojibake line references (e.g. "?175??76") — cannot serve as
   evidence.
2. Downgrade C10/A2 explicitly to hypothesis: forced-cut vs b_stt_error
   association is confounded and cannot be sign-off evidence for an attribution
   fix.
3. Add concrete test assertions: current-source must not contain prior whole
   chunks, but evidence-source may retain residual uncertainty.
4. Add raw runtime query/evidence, and note 1 of the 11 prior_overlap cases is
   `natural` — do not simplify to forced-only.
5. Explicitly choose or exclude the data-model direction; proposal points at
   Option B as more reasonable but has not written the implementation contract.

Conclusion: not for implementation now; Claude revises plan/scope/tests, then
Codex re-reviews whether blockers are resolved.

---

## §11 Revision Log (Step 3) — Claude, responding only to the Step-2 verdict

Per workflow: this section is additive; the Step-2 verdict above is preserved
unedited. Each point: ACCEPTED / REJECTED + reason.

| # | Codex point | Disposition | What changed / reasoning |
|---|-------------|-------------|--------------------------|
| 1 | Mojibake/unverifiable line refs | **ACCEPTED** | All line refs in C1/C3/C4 rewritten in plain ASCII against self-verified current line numbers: residual carry `sentence_buffer.py` 175-176 (block opens 162), `reset()` 104-112; `merge_cuts()` `sentence_splitter.py` 105-120 (concat 117-119); dedup `translator.py` 1055 / ~1180. Also corrected the prior wrong `merge_cuts` refs (was 68-83). |
| 2 | Downgrade C10/A2 to non-sign-off hypothesis | **ACCEPTED** | C10 and A2 now state in-line they are confounded (forced=harder audio), small N, and MUST NOT be used as sign-off evidence. |
| 3 | Concrete current-source vs evidence-source assertions | **ACCEPTED** | Added §8 "Concrete assertions" T1-T7 with explicit set relations (current-source excludes prior whole chunks; evidence-source retains them; no audio double-count; merge does not promote evidence->current). |
| 4 | Add raw runtime query + the 1 natural reuse case | **ACCEPTED** | Added the reproduction query to §6 step 5, and new claim C11 recording the reuse cut_reason breakdown (forced_blob 7 / forced_prefix 3 / natural 1). Scope no longer assumed forced-only until the natural case's path is confirmed (investigation steps 1/4). |
| 5 | Choose/exclude the data-model direction with a contract | **ACCEPTED (with a user-decision flag)** | §7 now explicitly excludes Option A-alone and C-alone (with Codex-confirmed reasons), recommends Option B, and writes a 6-point implementation contract. Because B is a data-model change, the proposal flags it as requiring explicit USER sign-off before implementation; a documented fallback (A+C minimal, knowingly accepting the 3b45025 regression) is included. The straddling-chunk handling (AS5) is named as an unresolved design point to settle before coding, not silently decided. |

New items surfaced during revision (not from Codex, flagged for re-review):
- N1. `sentence_splitter.py` has merge guardrails `_can_merge_cuts()` (lines
  40-54, `max_merge_source_count` / `max_merge_text_chars`) that bound C3's
  amplification — added to C3 and assertion A7. The original proposal omitted
  this.
- N2. The "natural cut = clean attribution" simplification used in earlier
  analysis is now explicitly retracted (C11): a natural-ending cut can inherit
  carried ids.

Open items remaining for Step-4 re-review:
- The data-model commitment (Option B) is a USER decision, not resolved here.
- AS5 (straddling-chunk current-vs-evidence) must be resolved before
  implementation.
- Investigation steps 1 and 4 (AS1 propagation confirmation, AS4 consumer map)
  remain prerequisites; this proposal does not assume their outcome.

Status: revised; not for implementation. Ready for Codex Step-4 re-review of
whether the five Step-2 points are resolved.

---

## Codex Re-Review (Step 4) — verbatim, DO NOT EDIT

Verdict: **PASS WITH NON-BLOCKERS.**

- All five Step-2 blockers RESOLVED (line refs ASCII/verifiable; C10/A2 marked
  hypothesis-only and forbidden as sign-off; T1-T7 concrete; raw query reran
  `412 11 Counter({'forced_blob': 7, 'forced_prefix': 3, 'natural': 1})`;
  Option A/C-alone excluded, Option B contract written and marked user-sign-off).
- No new true blockers. N1 (`_can_merge_cuts` guardrails, `sentence_splitter.py`
  line 40) and N2 (one natural reuse case, C11) verified; neither changes the
  verdict.
- Deferred items (Option B user decision, AS5, AS1 propagation, AS4 consumer
  map) correctly classified non-blocking.
- Non-blocking notes: decide AS5 before coding; complete AS4 consumer map
  (esp. `collection_sanity_report.py`, `sample_labeling_cases.py`, integration
  tests, labeling UI); update older annotation/sample wording that still calls
  `source_utterance_ids` an "evidence set."
- Final recommendation: proceed to user decision on Option B / AS5, then
  implementation planning.

---

## §12 User Decisions (Step 5) — locked

- **D-1 — Fix direction: Option B (split current-source vs evidence-source).**
  Excludes the A+C minimal fallback. Rationale: only option that stops the
  over-count without regressing 3b45025's evidence preservation.
- **D-2 — AS5: the boundary-straddling chunk is EVIDENCE-only.** Implementation
  rule becomes "on carry-forward, all accumulated chunks -> evidence; current
  starts empty and collects only post-carry new chunks." Benefit: requires no
  text->chunk offset map (does not depend on the D1 limitation). Consequence
  (accepted): a residual sentence's own residual-origin chunk is in
  evidence-source, so the labeling listening protocol must include evidence-source
  for residual-derived sentences; a pure-residual cut (no new chunk) has empty
  current-source (explicit pure-residual signal).

Carried into implementation planning (non-blocking, from Codex Step-4):
- AS4 consumer map (incl. `collection_sanity_report.py`,
  `sample_labeling_cases.py`, integration tests, labeling UI) to complete before
  coding.
- AS1 propagation confirmation (raw `translation` events) before coding.
- Option D (suppress pure-residual empty-current-source duplicates) is a separate
  subtitle-behavior decision, NOT in this task's scope.
- Cleanup older wording that calls `source_utterance_ids` an "evidence set" (it is
  now current-source; evidence moves to `evidence_source_utterance_ids`).

Status: decisions locked; contract complete. The next step (implementation) is
Codex's, triggered by a user-issued prompt per the workflow; this document does
not contain an implementation prompt.
