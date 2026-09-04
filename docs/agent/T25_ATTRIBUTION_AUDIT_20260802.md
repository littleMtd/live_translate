# T25 source-attribution audit — 2026-08-02

## Scope and decision boundary

This read-only audit reconstructs `WAV -> STT -> sentence/carry-forward ->
translation` for T25-056, T25-084, and T25-092. It tests the suspected common
attribution problem raised by the dual-ASR replay. It does not change the live
STT route, sentence policy, prompt, glossary, quality gate, or replay data, and
it does not promote human or alternate-ASR text to ground truth.

The three cases do not share one confirmed code cause. T25-056 exercises the
documented carry-forward contract. T25-084 and T25-092 have only current source
chunks and are ASR disagreements, not sentence-attribution failures.

## Provenance contract verified in code

- `SentenceEvent.source_utterance_ids` contains current STT chunks.
- `SentenceEvent.evidence_source_utterance_ids` contains earlier chunks that
  can still support carried residual text but are excluded from current audio
  duration and confidence totals.
- After a `forced_prefix` cut, the sentence buffer moves prior current IDs to
  evidence before accepting new current chunks. This is intentional because
  the pipeline has chunk-level provenance, not text-to-audio span offsets.
- Sentence merge and translation runtime emission preserve the two lists
  separately. The legacy singular `utterance_id` is only a correlation field;
  it is not full provenance.

Relevant contracts are in `modules/pipeline_events.py`,
`modules/sentence_buffer.py`, `modules/sentence_splitter.py`, and
`modules/translator.py`. Focused existing tests passed:

```text
23 passed in 2.20s
```

The command covered carry-to-evidence, pure residual, natural cut after carry,
sentence merge, and translator propagation. No new test or harness was added.

## Case reconstruction

### T25-056 — intentional carry-forward, not lost live provenance

- `utt-94` produced a 58-character STT result. Sequence 72 emitted a
  26-character forced prefix from it.
- The remaining text was carried. Sequence 73 then emitted with current IDs
  `utt-95, utt-96` and evidence ID `utt-94`.
- Both retained local ASRs begin their current-only text at
  `활용하기 어렵고...`; both recover the preceding
  `그러나 ... 다양한 상황에 활용하기 어렵고` material from the separately
  replayed evidence WAV `utt-94`.
- The declared 1.2-second overlaps for `utt-94 -> utt-95` and
  `utt-95 -> utt-96` are sample-for-sample identical. Audio was retained.

Conclusion: the live event records the contributor under the correct evidence
field. A consumer that reads only current IDs cannot reconstruct the full
assembled sentence, but that is a consumer/representation limitation, not an
ID-loss defect. Exact span attribution would require the deferred text-to-audio
offset work; this case alone does not justify that architecture change.

### T25-084 — one-chunk ASR disagreement

- Sequence 115 is a one-chunk `silence_complete` sentence from `utt-148`, with
  no evidence IDs and no overlap.
- Groq supplied `시청자 롤러가 챙겼다고요? 에이.` SenseVoice supplied
  `요료가 챙겼다고요 에.` and faster-whisper supplied
  `로러가 챙겼다고요? 에이`.
- Both local decoders omit `시청자`, but neither produces the human proposal
  `주르르가 찜했다고요`. The later `주르르` line is about 91 seconds and
  five translations away, so it is context evidence, not transcript proof.
- The WAV has a short quiet lead-in but no signal-level proof of a mid-word
  capture start.

Conclusion: carry-forward and annotation-join mismatch are falsified for this
case. Groq insertion/context influence, local-ASR omission, and an acoustically
unclear subject remain alternatives. Blinded listening is the cheapest
adjudication.

### T25-092 — weak-signal Groq/local-ASR disagreement

- Sequence 222 uses current IDs `utt-281, utt-282`, no evidence IDs, and a
  normal `silence_complete` cut.
- `utt-281` is a weak-signal outlier: raw RMS `0.002603` and Groq average
  log-probability `-0.7371297`. It passes the live rejection threshold `-1.0`
  but fails the stricter context-eligibility threshold `-0.7`.
- Therefore the immediately preceding eligible transcript was included in the
  Groq prompt for `utt-281`; after its weak result, context was gated for
  `utt-282` with reason `avg_logprob`.
- Groq emitted a first clause beginning `시청자님께서는...`. Both local ASRs
  independently emit an `어차피 스토리 밀다 보면 얘/예 뽑을 수 있어요`
  clause. Neither emits the human proposal `수수를 뽑을 수 있나요`.

Conclusion: sentence assembly faithfully concatenated the two current chunks.
The evidence supports a Groq recognition concern, but runtime telemetry proves
only that prior context was included, not that it caused the error. A
context-off Groq A/B would be a paid, separately authorized experiment; it is
not justified as the first step while the retained audio remains unadjudicated.

## Additional manifest finding

Annotation 68 exposed an offline provenance-manifest bug, not a live pipeline
loss. Its live translation sequence 20 has empty current IDs because it is a
pure carried residual, but it correctly records evidence IDs `utt-26, utt-27`.
Schema v1 of `data/semantic_quality_evidence_20260802.json` copied only current
IDs into `audio_refs`, then reported
`partial_selected_translation_missing_source_ids`. Schema v2 is rebuilt by
`scripts/rebuild_semantic_quality_provenance.py`: it preserves frozen selection
decisions, consumes both provenance lists, retains `source_kind`, and now joins
`utt-26,utt-27` to their unique successful STT rows and existing WAVs. The
selected missing-provenance count is zero.

## Result and next gate

The suspected shared T25-056/T25-084 attribution defect is falsified:

- T25-056: confirmed intentional carry-forward with complete two-list live
  provenance.
- T25-084: unresolved one-chunk ASR/audio disagreement.
- T25-092: weak-signal Groq/local-ASR disagreement with possible, unproven
  prompt-context influence.
- All three annotation joins are unique and aligned.

No production change follows from these three cases. The cheapest next
validation is a blinded token-level listen: the tail of `utt-94` plus the start
of `utt-95`, all of `utt-148`, and the first 4-5 seconds of `utt-281`. The
deterministic annotation-68 maintenance card is complete; it improves
evaluation integrity but does not improve live subtitle quality by itself.
