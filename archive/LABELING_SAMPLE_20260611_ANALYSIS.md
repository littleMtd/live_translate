# Labeling Sample 20260611 Analysis

## Scope

Source files:
- `logs/labeling_sample_20260611_153820.json`
- `logs/labeling_sample_20260611_153820.annotations.json`

Run: `20260611T035026Z-30856` (post-fix build: shared translator state, STT/LLM
context gates, segment-timestamp plumbing, silence_complete / gap-prefix cuts,
backpressure fixes, fail-fast NIM timeout + background probe).

Like the 20260531 sample, this is a real-world mixed-stream sample (clip audio,
BGM, speaker overlap included). It is compared against
`LABELING_SAMPLE_20260531_ANALYSIS.md` as the pre-fix baseline. The two streams
differ in content mix, so treat deltas as directional, not exact.

## Label Summary

Total labeled samples: 50 / 50

| Label | Count |
| --- | ---: |
| `ok` | 20 |
| `b_stt_error` | 19 |
| `a_translation_error` | 7 |
| `both` | 0 |
| `unclear` | 4 |

Derived buckets vs 20260531 baseline:

| Bucket | 20260531 (n=60) | 20260611 (n=50) | Delta |
| --- | ---: | ---: | ---: |
| Acceptable (`ok`) | 22% | **40%** | +18pt |
| STT-related (`b_stt_error` + `both`) | 55% | **38%** | -17pt |
| Translation-related (`a_translation_error` + `both`) | 23% | **14%** | -9pt |
| Unclear | 7% | 8% | ~0 |

All three buckets moved in the expected direction; `both` dropped to zero.
The +18pt / -17pt deltas clear the ~15pt signal threshold for n=50; the
translation-bucket delta is directional only.

## By Sentence cut_reason

| cut_reason | n | ok | STT-err | trans-err |
| --- | ---: | ---: | ---: | ---: |
| natural | 18 | 9 (50%) | 5 | 2 |
| silence_complete | 14 | 8 (57%) | 4 | 2 |
| forced_prefix | 6 | 0 (0%) | 5 | 0 |
| forced_blob | 5 | 1 | 1 | 2 |
| merged:forced_blob+silence_complete | 3 | 0 | 3 | 0 |
| merged (other) | 3 | 2 | 0 | 1 |
| forced_gap_prefix | 1 | 0 | 1 | 0 |

Key readings:
1. **silence_complete validated by ear**: ok-rate (57%) is on par with /
   slightly above natural cuts. The earlier concern that VAD-pause cuts would
   ship half-sentences did not materialize at scale. Keep the mechanism.
2. **Forced cuts remain the quality poison** (forced_prefix 0% ok), but their
   share fell from ~47% of sentences (baseline build) to ~24% in this sample.
   Most of the ok-rate gain comes from "less poison", not "better poison".

## Speaker Source Tags

| Tag | Count |
| --- | ---: |
| `host_only` | 26 |
| `clip_or_other_speaker` | 15 |
| `audio_source_mismatch` | 8 |
| `host_over_clip` | 7 |
| `wrong_speaker_selected` | 3 |
| `speaker_unclear` | 2 |
| `multi_streamer` | 1 |

host_only subset (n=26): ok 12 (46%), b_stt_error 8 (31%),
a_translation_error 5, unclear 1. Host-only speech performs clearly better
than the mixed-stream remainder; remaining STT errors concentrate in
clip/multi-speaker audio, which is a speaker-policy problem, not a pipeline
bug.

## Sample IDs

By label:
- `ok`: S001, S002, S003, S009, S010, S011, S015, S016, S017, S019, S020, S023, S032, S038, S040, S044, S047, S048, S049, S050
- `b_stt_error`: S005, S006, S008, S013, S014, S018, S021, S022, S024, S025, S029, S030, S033, S035, S036, S039, S042, S043, S045
- `a_translation_error`: S012, S026, S027, S028, S034, S037, S041
- `unclear`: S004, S007, S031, S046

By context tag:
- `clip_audio`: S001, S015, S016, S017, S019, S024, S025, S027, S029, S031, S039, S041, S045, S048, S049
- `bgm_mixed`: S001, S003, S015, S025, S030, S031, S033, S034, S035, S036, S039, S047, S050
- `multi_speaker`: S004, S013, S015, S018, S024, S029, S033, S035, S036, S039, S041
- `over_attributed_chunks`: S004, S008, S014, S021, S022, S042, S045, S046
- `unclear_audio`: S004, S007, S024, S025, S029

Follow-up shortlist — host_only translation errors (candidates for
glossary/prompt fixes): S012, S026, S028, S034, S037.

## Interpretation

The fix campaign between 20260531 and 20260611 (state sharing, context gates,
timing-based cuts, backpressure and NIM-degradation guards) produced a broad,
same-direction improvement: ok 22% -> 40%, STT bucket 55% -> 38%, translation
bucket 23% -> 14%. The dominant mechanism is the reduction of forced cuts
(47% -> ~24%) via silence_complete, which itself labels as well as natural
cuts.

Remaining error mass:
1. Mixed-stream audio (clip / multi-speaker / host-over-clip) — requires a
   speaker policy decision before any diarization/speaker-filter work.
2. Five host_only translation errors (S012, S026, S028, S034, S037) — review
   individually; likely glossary/prompt-fixable.
3. forced_prefix is still 0% ok — further pressure on forced cuts (e.g.
   strengthening gap-prefix splitting, which fired only once here) is the next
   pipeline-side lever.

## Recommended Next Work

1. Decide the speaker policy (translate host only / dominant speaker / as
   heard). This gates the largest remaining error bucket.
2. Review the five host_only translation errors for glossary/prompt fixes.
3. Keep `silence_complete` enabled; revisit `forced_prefix` pressure
   (segment-gap thresholds) in a future tuning pass.
4. Next labeling round: same label set, prefer a host-only-heavy stream to
   isolate pipeline quality from speaker-mix noise.
