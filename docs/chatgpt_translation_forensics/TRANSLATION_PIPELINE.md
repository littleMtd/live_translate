# Translation Pipeline and Ownership

Use the uploaded events to establish actual behavior; configuration defaults
describe possibilities, not proof that a route ran.

## 1. Capture and STT

`audio_startup` and `audio` records describe device readiness, VAD/chunk
decisions, durations, overlap, and discard reasons when persisted. `stt` records
describe request/result status, provider/model attribution, latency, filtering,
confidence aggregates, context provenance, and utterance identifiers where
available.

STT output is the source observed by downstream code. It is not guaranteed to
match the audio. If a retained WAV exists, listening may support a stronger
claim; otherwise distinguish source uncertainty from a translation defect.

## 2. Sentence assembly

Sentence-buffer logic combines accepted transcription events, tracks
discontinuities, and emits bounded sentence cuts. Correlate `utterance_id` and
source/evidence ID lists into `sentence` events. A sentence may merge multiple
utterances. Cut reason, incompleteness, timing, profile snapshot, and activity
snapshot can affect the request without changing what was acoustically spoken.

## 3. Frozen translation request

For each translation, identify:

- `(run_id, sequence_id)` and `sentence_id`;
- source and evidence utterance IDs;
- source text actually sent downstream;
- effective profile, profile generation, evidence source, and activity;
- history cohort/count and cache identity/status;
- prompt/version and request disposition where present.

Sequence IDs are only unique within a run. Do not join across runs using a bare
sequence number.

## 4. Provider attempts and fallback

Inspect the complete attempt chain, not only the selected engine. Each attempt
may carry provider/model/route, phase, status, latency, token/cost data, raw
candidate output if retained, correction trace, and output-guard disposition.

A provider failure, content rejection, fallback advance, and selected success
are different events. A rejected candidate is not the published subtitle. Use
the selected-attempt marker and final translation fields to determine ownership.

## 5. Deterministic finalization

The finalizer owns protected-span restoration, source-aware corrections,
canonical/name/terminology occurrence checks, script/meta/content guards, and
the final fail-closed decision. Diagnostic flags describe detected properties;
they do not by themselves prove user-visible semantic failure.

When text differs between raw provider output and final target, use retained
correction and guard metadata to attribute the transformation. Do not infer a
transformation that was not logged.

## 6. Provisional lifecycle

Trace `provisional_id`, action/status, fingerprint disposition, subtitle ID,
and revision. A provisional display may later be replaced by a final revision.
Exact promotion and ordinary retranslation share final correctness invariants,
but follow different candidate histories.

## 7. Publication

Use final translation status, `subtitle_emitted`, suppression reason,
`sequence_id`, provisional/final identifiers, and subtitle lifecycle events
where present. A successful provider response is not necessarily publication.
Conversely, the absence of a general display event does not prove non-display
when publication evidence is carried by the translation event.

## Practical correlation sequence

```text
published subtitle
  -> translation event and selected attempt
  -> sentence event and source/evidence utterances
  -> STT and optional retained WAV
  -> captured profile/activity generation
  -> history/cache cohort
  -> deterministic correction/guard evidence
  -> provisional/final display lifecycle
```
