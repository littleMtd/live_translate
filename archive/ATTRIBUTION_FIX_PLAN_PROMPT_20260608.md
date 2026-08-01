# Claude Code Proposal Prompt: Forced-Cut Source Attribution Review

You are working in `c:\Users\c0737\Pictures\code\live_translate`.

Draft a neutral proposal for the project's 7-step workflow:
Claude Code proposal -> Codex cross-review -> Claude revision if needed -> Codex re-review -> implementation only after blockers are resolved or the user explicitly proceeds -> post-implementation review.

Do not edit code. Do not implement. Do not stage or commit. This step is proposal/root-cause review planning only.

## Objective

Verify and plan a fix for a suspected forced-cut source attribution over-count issue. Do not assume the issue is fully proven or that it explains all STT errors.

The working concern is:

> Runtime `translation` events can carry prior `source_utterance_ids` into later forced-prefix/forced-blob translations. This may contaminate labeling audio/source evidence and make STT-vs-translation labels unreliable for affected samples.

Use neutral wording such as `suspected attribution over-count` or `forced residual attribution issue`, not broad wording like `the attribution bug caused the STT errors`.

## Required Proposal Format

Use a proposal format, not persuasive wording.

Include:

1. Goal and problem statement.
2. Claims with IDs, each with evidence type: `code`, `runtime`, `audit`, `user decision`, or `assumption`.
3. Scope.
4. Non-goals.
5. Assumptions and how to verify/falsify each.
6. Proposed investigation steps before implementation.
7. Candidate implementation approaches, with tradeoffs.
8. Test plan.
9. Runtime validation plan.
10. Reviewer checklist listing claims to validate, not confirmation questions.

Do not write conclusion-framing phrases like `no open decisions`, `obvious`, `low risk`, or `reviewer should agree`.
Do not pre-fill reviewer answers.
Ask Codex to verify claims against code/runtime evidence.

## Background Evidence

### First Human Annotation Round

Sample: 60 cases.

Labels:

- `b_stt_error`: 29
- `both`: 4
- `ok`: 13
- `a_translation_error`: 10
- `unclear`: 4

Context tags:

- `multi_speaker`: 30
- `clip_audio`: 10
- `bgm_mixed`: 11
- `unclear_audio`: 7
- `over_attributed_chunks`: 5

### Second Speaker/Source Annotation Round

Sample: 33 cases.

Labels:

- `b_stt_error`: 27
- `both`: 2
- `ok`: 2
- `a_translation_error`: 2

Context tags:

- `multi_speaker`: 23
- `clip_audio`: 10
- `over_attributed_chunks`: 8
- `bgm_mixed`: 4
- `unclear_audio`: 4

Interpretation caution:

- These rounds show STT/source/audio issues are prominent.
- They do not prove all STT errors come from source attribution.
- Multi-speaker, clip audio, BGM, and unclear audio remain separate failure classes.

### Current Runtime Evidence

Run: `20260606T095302Z-42260`

Population:

- `translation`: 412
- `multi_chunk`: 198
- forced cut total: 195
- prior-overlap samples: 11
- low-confidence samples: 73
- high `no_speech` samples: 11
- filtered/template cases: 32
- audio/STT/wav joins complete

Observed repeated source-id examples:

1. `seq 163` used `utt-275/utt-276/utt-277`; `seq 164` used `utt-275/utt-276/utt-277/utt-278/utt-279`.
   - First three were shown as `prior_overlap` for `seq 164`.

2. `seq 393` used `utt-654/utt-655`; `seq 394` used `utt-654/utt-655/utt-656`.
   - First two were shown as `prior_overlap` for `seq 394`.

3. `seq 48` used `utt-89/utt-90/utt-91`; `seq 49` reused the same `utt-89/utt-90/utt-91` for source text `최대 체력`.

User listening notes:

- In several affected cases, the new/primary chunk matches the displayed sentence text.
- The inherited prior chunks contain previous sentence speech.
- Some inherited overlap is whole chunks or several seconds, not just normal VAD overlap.
- Some cases still have the main source and translation acceptable despite prior audio pollution, so labels must distinguish user-visible subtitle quality from attribution quality.

Clean-speech observation:

- When interference disappears and only one clear speaker remains, Groq Whisper STT appears reasonably accurate.
- Do not frame this task as replacing STT or adding diarization.

## Code Areas To Review

Read and cite exact file/line references yourself:

- `modules/sentence_buffer.py`
  - `SentenceBuffer.pop_ready()`
  - forced branch
  - `forced_prefix` residual handling
  - source/audio/confidence tally behavior

- `modules/sentence_splitter.py`
  - `emit_cut()`
  - `merge_cuts()`
  - merge guardrails
  - source-id concatenation

- `modules/pipeline_events.py`
  - `SentenceEvent`
  - `transcription_to_sentence()`
  - `sentence_metadata()`
  - `source_confidence_summary()`

- `modules/translator.py`
  - how `sentence_metadata()` is copied into translation runtime events
  - whether translator performs duplicate suppression by source IDs or only output text

- `scripts/sample_labeling_cases.py`
  - whether `source_chunks[]` and `source_chunk_usages[]` are reconstructed from runtime event source IDs or created independently
  - prior-overlap role detection

- Tests:
  - `tests/test_sentence_buffer.py`
  - `tests/test_sentence_splitter.py`
  - `tests/test_pipeline_events.py`
  - `tests/test_labeling_review_server.py`

Relevant history:

- `git show 3b45025 -- modules/sentence_buffer.py`

## Questions The Proposal Must Address

1. Are runtime `translation` events themselves carrying stale or over-counted `source_utterance_ids`, or is this only sample reconstruction/UI behavior?

2. Is source-id reuse expected for normal VAD overlap? If yes, what distinguishes reasonable short overlap from unacceptable whole-chunk over-attribution?

3. Does `SentenceBuffer.pop_ready()` carry source/audio/confidence tallies forward after `forced_prefix` residual handling?

4. If yes, was that behavior intentionally introduced to avoid dropping residual source evidence?

5. Why is a one-line reset after forced prefix insufficient?

6. Does `sentence_splitter.merge_cuts()` amplify carried/duplicated source IDs?

7. Should the fix change live subtitle behavior, labeling attribution only, or the data model separating current-source vs evidence-source?

8. What tests would fail under the desired new behavior, and which existing tests encode the old tradeoff?

## Candidate Directions To Evaluate

Do not choose before reviewing code. Evaluate tradeoffs.

### Option A: Reset source tallies after forced-prefix residual

Potential upside:

- Prevents prior chunks from being attributed to residual-derived sentences.

Potential downside:

- Can drop evidence when a single STT chunk straddles the punctuation boundary.
- May regress the issue addressed by commit `3b45025`.

### Option B: Carry residual text but mark carried source IDs as evidence-only / uncertain

Potential upside:

- Avoids treating old chunks as current sentence audio.
- Preserves evidence that residual may derive from prior chunk.

Potential downside:

- May require data-model changes beyond `source_utterance_ids`.
- Tests and sample UI must distinguish current-source from evidence-source.

### Option C: Keep runtime behavior but adjust labeling/sample interpretation

Potential upside:

- Smaller live-behavior change.

Potential downside:

- Does not fix runtime event attribution if downstream tools rely on `source_utterance_ids`.

### Option D: Add duplicate source-span suppression at translator/sentence stage

Potential upside:

- Can prevent residual fragments such as seq 48/49 duplicate-source events.

Potential downside:

- Could suppress legitimate short follow-up translations unless criteria are precise.

## Test Expectations

The proposal should include tests that demonstrate:

- A forced-prefix residual case no longer attributes the full prior source span as current-source audio for the next sentence.
- Evidence/confidence is not silently dropped when residual text may still derive from a straddling chunk.
- Runtime `translation` events expose enough structure for labeling to distinguish current source chunks from prior/evidence chunks.
- `sample_labeling_cases.py` labels prior overlap based on runtime evidence without inventing chunks.
- Existing clean/natural cuts preserve attribution behavior.
- Merge guardrails do not amplify duplicated/carry-forward source IDs.

Suggested focused commands after implementation, to be refined by the proposal:

```powershell
.\live-subtitle-env\Scripts\python.exe -m pytest tests\test_sentence_buffer.py tests\test_sentence_splitter.py tests\test_pipeline_events.py tests\test_labeling_review_server.py
.\live-subtitle-env\Scripts\python.exe -m pytest tests\test_integration.py tests\test_collection_sanity_report.py tests\test_sample_labeling_cases.py
```

## Runtime Validation Expectation

After implementation, run a replayable `LIVE_TRANSLATE_DUMP_AUDIO=1` collection and regenerate a sample.

Validation should compare:

- repeated source-id rate between consecutive translations
- prior-overlap sample count
- forced cut count and source-span behavior
- label reliability for STT-vs-translation classification
- clean-speech STT quality remains unchanged

Do not claim the fix improves multi-speaker, clip audio, BGM, or song/humming cases unless validated separately.

## Desired Output

Return the proposal only. Do not implement.

