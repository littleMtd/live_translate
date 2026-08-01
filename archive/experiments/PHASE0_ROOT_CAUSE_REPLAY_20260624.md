# Phase 0 Root-Cause Replay Baseline (2026-06-24)

## Scope

This is an offline replay baseline for the frozen 30-case host-primary evaluation set. It does not modify or call the live translation path. The sample is stratified and problem-oriented; none of the ratios below are global production rates.

Inputs:

- `logs/labeling_sample_phase0_eval_20260613_host_primary_pilot10.json`
- `logs/labeling_sample_phase0_eval_20260613_host_primary_batch2_20.json`
- `logs/labeling_sample_phase0_eval_20260613_host_primary.annotations.json`

Generated artifacts:

- `.analysis-tmp/phase0_replay_manifest_20260624.json`
- `.analysis-tmp/phase0_sensevoice_shadow_20260624.json`
- `.analysis-tmp/phase0_routing_spans_20260624.annotations.json`
- `.analysis-tmp/phase0_routing_span_summary_20260624.json`
- `.analysis-tmp/phase0_alert_shadow_dataset_20260624.json`
- `.analysis-tmp/phase0_alert_shadow_evaluation_20260624.json`
- `.analysis-tmp/phase0_speaker_similarity_shadow_20260624.json`
- `.analysis-tmp/phase0_short_overlap_dedupe_shadow_20260624.json`

Reproducible commands:

```powershell
.\live-subtitle-env\Scripts\python.exe scripts\build_phase0_replay_manifest.py
.\live-subtitle-env\Scripts\python.exe scripts\replay_phase0_stt_candidates.py
```

## Provenance Manifest

`scripts/build_phase0_replay_manifest.py` validates completed annotations, unique sample IDs, `speaker_policy=host-primary`, and the presence of every source WAV. It records size and SHA-256 as the expected audio fingerprint. `scripts/replay_phase0_stt_candidates.py` rechecks both before inference and fails if a WAV changed. The manifest preserves each candidate's complete `source_chunk_usages`, current/evidence utterance IDs, runtime diagnostics, translation status, filter reason, and quality metadata.

Validated inventory:

- Cases: 30
- Source WAV assets: 53
- `source_routing`: 9
- `clean_host_stt`: 2
- `stt_unverified`: 1
- `translation`: 3
- `filter_policy`: 1
- `control_ok`: 11
- `unclear`: 3

The programmed groups do not upgrade annotation certainty. Every case also carries `ground_truth_status`:

- `exact_heard_source`: an exact heard-source transcript exists
- `correction_or_note_only`: only a corrected span or explanatory note exists
- `human_judgment_only`: the label is a human judgment but has no reproducible transcript/correction artifact

In the STT subset, `S052` and `S062` are `human_judgment_only`; `S063` is `correction_or_note_only`. There is no exact heard-source transcript for these cases.

## SenseVoice Shadow

Only `clean_host_stt` and `stt_unverified` were sent to local SenseVoice. Source-routing cases were deliberately excluded because changing STT engines cannot identify the intended speaker in a mixed loopback signal. Current-source and carry-forward evidence transcripts are emitted separately; evidence audio is never concatenated into the candidate compared with current `source_text`.

| Case | Groq/current source | SenseVoice candidate | Result |
|---|---|---|---|
| `S063` | `...크레이지 아케이드 썹쥬 한다고요?...` | `...크레이즈 아케이드 서핑 한다고요...` | Note says `섭종`; this is context-resolvable and is not a valid pure-acoustic dual-STT test. |
| `S052` | Contains `낮방`, `미음남`, `읍이라는` | Contains `나방`, `미음놈`, `이게읍이라는` | Engines disagree, SenseVoice reports BGM, and no exact reference exists. Not scorable. |
| `S062` | `어, 고정 걸어놨었어` | `어 고정 걸어놨었어.` | Engines agree, but no correction/reference was recorded. The original B label is not reproducible from artifacts; excluded as `stt_unverified`. |

Post-load local inference latency on an RTX 3050 Laptop GPU:

- `S052`: 2219 ms total for two chunks (15.9 s audio)
- `S062`: 546 ms for one chunk (4.1 s audio)
- `S063`: 1047 ms for one chunk (11.2 s audio)

Model load time and live GPU contention were not measured. SenseVoice emitted BGM metadata for `S052` and `S062`, and Speech metadata for `S063`; these tags are not host-speaker attribution signals.

This run is underpowered and cannot test the dual-STT hypothesis. It must not be read as evidence that dual-STT is ineffective: there are no exact references, `S063` tests contextual resolution rather than a clean acoustic alternative, and SenseVoiceSmall capability is confounded with the general value of a second STT engine.

## Classification Caveats

- `S053` remains labeled `a_translation_error` in the immutable human annotation, but the derived root-cause group is `filter_policy`: `status=filtered`, `filter_reason=stt_garbage`, and no translation API call occurred.
- `S026` and `S049` remain `unclear`. Their singing notes are insufficient to prove that suppression was correct; they must not be silently converted to `control_ok`.
- In the initial manifest, `S003` is `host_only + audio_source_mismatch`, so the case-level precedence rule places it in `source_routing`. The later time-span ground truth supersedes that grouping for mechanism analysis: both spans are host, and `S003` is removed from the conceptual routing-error numerator. The immutable original annotation and manifest grouping remain unchanged for provenance.

## Initial Source-Routing Replay Gap (Resolved for These Nine Cases)

At the manifest stage, the nine source-routing annotations were case-level. Runtime `source_chunk_usages` says which chunks the current system treated as primary, supporting, or prior-overlap evidence, but it is not ground truth. Before the time-span annotation pass, the data did not identify, by time span within the original WAV:

- host speech
- valid clip/game speech while host is silent
- donation/subscription alert or TTS
- mixed host plus non-host speech
- the exact source span that should have produced the subtitle

Therefore the initial manifest could reproduce the failure and inspect its provenance, but could not compute router precision, recall, or overlap-rescue rate. Treating runtime attribution as expected attribution would train and evaluate against the same bug. The time-span annotation described below resolves this labeling gap for these nine cases only; it does not establish production prevalence or provide enough cases for a general router benchmark.

Runtime chunk IDs may be retained as containers/provenance, but expected source labels must be anchored to start/end offsets. Otherwise forced cuts, carry-forward, and over-attribution define the evaluation boundary using the behavior being evaluated.

## Initial Decision Before Routing Annotation

1. Dual-STT is **not testable from the current labeled artifacts**. Keep implementation deferred because there is no measured benefit, not because this run disproved it.
2. Do not run source-routing cases through more STT engines and call disagreement a speaker signal. Both engines transcribe the same mixed audio.
3. Keep source-routing and clean-host STT as separate gates. The current 30-case set is useful as a regression fixture, not as a rescue-rate denominator.
4. Routing gate: annotate WAV time spans as `host`, `content_other`, `alert_tts`, `mixed`, or `unrelated`, plus the expected selected span. Do not treat runtime chunk boundaries as ground truth. This gate was completed later on 2026-06-24 for all nine cases.
5. Acoustic/resolver gate: separately record exact `heard_source_text` or explicit corrected spans for clean-host STT cases. Context-resolvable and pure-acoustic cases must be reported separately.
6. Until the relevant ground truth exists for each mechanism, prioritize deterministic fixes already evidenced by the set (filter false positives, contextual term normalization, diagnostics) over multi-STT architecture. Routing time-span ground truth now exists for the nine routing cases; exact acoustic transcripts remain missing for the clean-host STT cases.

## Verification

```text
8 passed: test_build_phase0_replay_manifest.py + test_replay_phase0_stt_candidates.py
```

## Routing Time-Span Ground Truth Tool

Implemented on 2026-06-24 as a separate annotation surface. It reads only the nine `source_routing` cases from the replay manifest and writes `.analysis-tmp/phase0_routing_spans_20260624.annotations.json`; it does not modify the original 30-case labels.

```powershell
.\live-subtitle-env\Scripts\python.exe scripts\routing_span_review_server.py --port 8765
```

Schema:

- Time coordinates are offsets within each fingerprinted WAV. Runtime chunk IDs are provenance containers, not expected routing boundaries.
- `source_class`: `host`, `content_other`, `alert_tts`, `mixed`, `unrelated`, `uncertain`.
- `routing_action`: `translate`, `extract_host`, `extract_content`, `suppress`, `context_only`, `exclude`. Use `uncertain + exclude` instead of guessing; excluded spans do not enter router metrics.
- Draft annotations may have gaps. A case can become `complete` only when every WAV has non-overlapping spans covering its full duration within 50 ms tolerance.
- The annotation file stores the replay-manifest SHA-256. Startup fails if the manifest changed, and audio serving/replay fails if a WAV no longer matches its recorded size/SHA-256.

Verification after adding this tool:

```text
13 passed: replay manifest + STT replay + routing span annotation/API tests
9 routing cases / 18 WAV assets loaded from the real manifest
```

## Routing Span Results (2026-06-24)

All nine cases and 18 WAV assets are complete. The annotation contains 51 non-overlapping spans over 137.100 seconds. `uncertain + exclude` covers 0.386 seconds (0.28%); 136.714 seconds are evaluable. This is a stratified mechanism sample and the durations are not production prevalence estimates.

Mechanism split:

- `overlap_extraction` (3): `S007`, `S011`, `S045`
- `sequential_alert_suppression` (3): `S012`, `S017`, `S040`
- `host_only_no_audio_routing` (2): `S003`, `S031`
- `suppress_non_speech` (1): `S041`

Oracle action duration:

- `translate`: 84.482 s
- `extract_host`: 20.752 s
- `extract_content`: 13.652 s
- `suppress`: 17.828 s
- `exclude`: 0.386 s

Implications:

1. Only three of the nine case-level routing errors require overlap extraction. A full speech-separation architecture is not the first fix for every routing-tagged case.
2. Three cases are sequential alert/TTS insertion and can first be attacked with alert detection plus suppression, without separating simultaneous speakers.
3. `S003` and `S031` contain only host spans under the time-span ground truth. Their prior `audio_source_mismatch`/routing grouping does not describe competing audible speakers; investigate attribution, segmentation, or source-text assembly separately.
4. `S041` is suppressible non-speech rather than a target-speaker selection problem.
5. The resulting investigation order was: deterministic alert/non-speech suppression shadow, attribution/assembly audit for host-only mismatches, then target-speaker extraction shadow for the three true-overlap cases. The first two investigations are completed below; target-speaker extraction remains deferred.

Machine-readable summary: `.analysis-tmp/phase0_routing_span_summary_20260624.json`, generated by `scripts/summarize_phase0_routing_spans.py`.

## Alert / Non-Speech Suppression Shadow

The non-overlap gate contains 21 evaluable spans from eight cases:

- host/pass: 15
- alert TTS/suppress: 5
- unrelated/suppress: 1
- mixed and uncertain spans are excluded from this gate

Artifacts:

- `.analysis-tmp/phase0_alert_shadow_dataset_20260624.json`
- `.analysis-tmp/phase0_alert_shadow_evaluation_20260624.json`
- `.analysis-tmp/phase0_speaker_similarity_shadow_20260624.json`

Cheap acoustic features (`dbfs`, zero-crossing rate, spectral centroid/flatness, active-frame ratio) are insufficient under leave-one-case-out evaluation:

- balanced accuracy: 0.650
- suppress recall: 0.500
- host pass recall: 0.800

CAM++ target-speaker similarity is more informative but still unsafe as a suppress decision:

- best training-balanced LOCO threshold: balanced accuracy 0.900, suppress recall 1.000, host pass recall 0.800 (3/15 host spans falsely suppressed)
- safety-first LOCO threshold below every training-host score: balanced accuracy 0.717, suppress recall 0.500, host pass recall 0.933 (still falsely suppresses `S011:utt-3602:2`)
- post-load inference latency on RTX 3050 Laptop GPU: mean 130.2 ms, range 47-234 ms per span
- pinned CAM++ model SHA-256: `3388cf5fd3493c9ac9c69851d8e7a8badcfb4f3dc631020c4961371646d5ada8`

Decision:

1. Do not connect either cheap features or CAM++ similarity to live suppression. A host false-suppression rate of 6.7-20% on this small set is unacceptable.
2. CAM++ similarity may remain a diagnostic/shadow feature, but it cannot be the sole router. The low-similarity host outlier in `S011` demonstrates that a host centroid is not a safe binary gate under voice variation/mixing.
3. Do not tune a duration or margin rule on these same 21 spans; that would overfit the labeled test set.
4. This result led to the `S003`/`S031` attribution and source-assembly audit below. Their time-span GT is host-only, so speaker suppression cannot address their observed failure.

## Host-Only Attribution / Assembly Audit

`S003` and `S031` are not audio-routing failures under the time-span ground truth, but they also do not share one root cause.

### S003: short overlap text survives both dedupe layers

- The previous subtitle (sequence 175) ends with `메이플?`; sequence 176 begins with the same `메이플?`.
- The first current WAV (`utt-234`) carries 0.4 s of audio overlap. Both source utterances are first-seen current input; there is no carry-forward evidence or wrong-speaker span.
- Runtime diagnostics report `timestamp_deduped_segments=0` and `timestamp_deduped_chars=0`. The first Groq segment therefore was not wholly inside the timestamp cutoff; segment-level dedupe could not remove only its leading text.
- The fallback text dedupe also preserves the prefix. `메이플?` is four characters including punctuation, while `dedupe_transcript_overlap()` requires at least five characters or two tokens.
- Replaying the exact adjacent text with `min_overlap_chars=4` removes the duplicate and retains `그거 살짝 그거잖아 근데.`.

The best-supported mechanism is an overlap-boundary duplicate, not speaker selection. This is a deterministic candidate fix, but lowering the global threshold directly would risk deleting intentional short repetitions.

The offline shadow over 2026-06-13 through 2026-06-19 runtime events found:

- 4,644 successful translations scanned
- 24 subtitles (0.52%) newly changed by lowering the exact character overlap threshold from five to four, only when the incoming source WAV reports audio overlap
- 14 candidates for `hades_chxxnnx`, 10 for `isegye_lilpa`
- `S003` is reproduced: `메이플?` is removed from sequence 176

Artifact: `.analysis-tmp/phase0_short_overlap_dedupe_shadow_20260624.json`, generated by `scripts/evaluate_short_overlap_dedupe_shadow.py`.

This establishes non-zero production reach, not safety. The 24 candidates still need audio review for intentional repetition before any live-path change. A passing gate requires zero confirmed host-content deletions; otherwise retain the current threshold or add a more specific boundary condition.

### S031: clean-host acoustic STT, exact error unverified

- Both WAV spans are `host + translate`; there is no alert, clip speaker, evidence carry-forward, or neighboring-text duplication.
- `utt-2350` has `avg_logprob=-0.742`; `utt-2351` has `avg_logprob=-0.486` and `no_speech_prob=0.331`. The observed source is therefore a low-confidence acoustic STT candidate.
- The annotation has no `heard_source_text` or correction. The exact recognition error cannot be reproduced or scored, so this is not evidence for a specific resolver, second STT, or confidence threshold.

Derived disposition: `clean_host_stt_unverified`. Keep the case out of routing precision/recall and out of acoustic rescue-rate denominators until exact heard-source ground truth exists. Confidence diagnostics may prioritize it for review, but must not silently suppress it.

### Audit decision

1. Remove `S003` and `S031` from the conceptual routing-error numerator; their immutable original annotations remain unchanged for provenance.
2. Audio-review the 24 overlap-conditioned shadow candidates before any live-path change.
3. Do not implement a special-case correction for `S031`; the necessary acoustic ground truth is absent.
4. The remaining true routing work is three sequential alert cases, three overlap-extraction cases, and one non-speech suppression case. CAM++ remains diagnostic-only.

## Mechanical Corroboration of Decision-Critical Labels (2026-06-24)

The routing/suppress labels are single-listener. They were compared with mechanical signals already in the artifacts (read-only; no live path), but this is not an independent ground truth and does not substitute for a second human annotator. CAM++ uses host examples selected from the same labeled dataset, and its measured host false-suppression rate prevents treating model agreement as confirmation.

Available independent signals:

- CAM++ `host_similarity` over the 21 non-overlap-gate spans (`.analysis-tmp/phase0_speaker_similarity_shadow_20260624.json`). The host centroid is built leave-one-case-out, so it is independent of the span's own human label.
- Audio `overlap_seconds` and timestamp-dedupe diagnostics from `logs/runtime_events_20260613.jsonl`.
- Not available in this data: a platform donation/alert event stream (the log carries only `audio`/`stt`/`sentence`/`translation`), and a diarization/source-separation pass over the three overlap cases (only single-embedding host similarity was run).

Mechanically consistent observations:

- All six `suppress` spans (five `alert_tts`, one `unrelated`) have low CAM++ `host_similarity` (0.183-0.406). This is consistent with the human non-host labels, but it does not remove their single-listener status. CAM++ also cannot distinguish `alert_tts` from other non-host audio.
- Both `S003` spans have high host similarity (0.794 and 0.714), consistent with the human host-only labels. The duplicated `메이플?` also occurs on `utt-234`, whose runtime `overlap_seconds=0.4`. Together these signals support treating overlap dedupe as the leading mechanism candidate, but the 24-case audio review remains the safety gate before calling it confirmed or changing the live path.

Re-listen list (mechanical signal disagrees with the human host/pass label):

- `S011:utt-3602:2` — `host_similarity=0.190`, a strong outlier under both reported thresholds. This is compatible with overlap contamination, but does not by itself prove the `overlap_extraction` label.
- `S031:utt-2350:2` — `host_similarity=0.461` and `avg_logprob=-0.742`. It is suppressed only by the training-balanced threshold (~0.475), which already produces three host false suppressions; it passes the safety-first threshold (~0.312). Re-listen is warranted, but this is not evidence that the span is non-host.
- `S017:utt-2300:3` — `host_similarity=0.462`. It is also suppressed only by the unsafe training-balanced threshold (~0.482) and passes the safety-first threshold (~0.317).
- Caveat: the training-balanced CAM++ threshold over-suppresses (host-pass recall 0.800, 3 false suppressions), so disagreement under that threshold is only a review-priority signal.

Residual second-annotator gaps (not corroborable from existing data):

1. `alert_tts` vs other mechanism sub-labels — no independent platform alert/donation event source exists in the logs. Forward fix: log platform donation/alert events so future alert labels have an independent timestamp source.
2. The three overlap cases' simultaneous two-speaker claim — needs an explicit diarization/source-separation shadow with host enrollment. A single CAM++ embedding over already mixed audio cannot identify both sources or establish extractability.

## Current Phase 0 Gates

1. **Short overlap dedupe:** 24 candidates from 4,644 successful translations are awaiting audio review in `.analysis-tmp/phase0_short_overlap_dedupe_20260624.annotations.json`. No live-path threshold change is allowed until the review finds zero confirmed host-content deletions.
2. **Sequential alert/non-speech suppression:** blocked from live integration. Cheap features are inadequate, and CAM++ alone has an unacceptable host false-suppression rate.
3. **True overlap extraction:** three labeled cases (`S007`, `S011`, `S045`) justify a future extraction/diarization shadow, but not production implementation or a general rescue-rate claim.
4. **Clean-host acoustic rescue:** still not testable. Exact `heard_source_text` ground truth is missing for the decision-critical cases; `S031` remains `clean_host_stt_unverified`.
5. **Dual-STT:** remains deferred for lack of a scorable clean-host acoustic set, not because SenseVoice disproved the architecture.
