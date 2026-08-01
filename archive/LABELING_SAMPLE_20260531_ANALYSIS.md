# Labeling Sample 20260531 Analysis

## Scope

Source files:
- `logs/labeling_sample_20260531_232759.json`
- `logs/labeling_sample_20260531_232759.annotations.json`

This sample should be treated as a real-world mixed-stream quality sample. It
includes Chxxnnx speech, clip/other-streamer speech, speaker overlap, donation or
music interference, and chunk attribution issues. It should not be used alone as
a host-only Chxxnnx STT quality estimate.

## Label Summary

Total labeled samples: 60 / 60

| Label | Count |
| --- | ---: |
| `b_stt_error` | 29 |
| `ok` | 13 |
| `a_translation_error` | 10 |
| `both` | 4 |
| `unclear` | 4 |

Derived buckets:
- STT-related errors (`b_stt_error` + `both`): 33 / 60
- Translation-related errors (`a_translation_error` + `both`): 14 / 60
- Acceptable (`ok`): 13 / 60
- Unclear (`unclear`): 4 / 60

## Context Tags

| Tag | Count |
| --- | ---: |
| `multi_speaker` | 30 |
| `bgm_mixed` | 11 |
| `clip_audio` | 10 |
| `unclear_audio` | 7 |
| `over_attributed_chunks` | 5 |

Important intersections:
- `multi_speaker` + STT-related error: 22 / 30
- `clip_audio` + STT-related error: 9 / 10
- `over_attributed_chunks` + STT-related error: 5 / 5

## Sample IDs

By label:
- `b_stt_error`: S001, S004, S007, S008, S010, S016, S018, S019, S020, S021, S023, S026, S027, S031, S032, S039, S040, S041, S042, S044, S045, S046, S047, S049, S050, S051, S052, S054, S060
- `ok`: S003, S005, S009, S012, S017, S024, S025, S028, S033, S035, S037, S038, S055
- `a_translation_error`: S011, S022, S029, S030, S034, S043, S048, S056, S057, S058
- `both`: S002, S006, S053, S059
- `unclear`: S013, S014, S015, S036

By context tag:
- `multi_speaker`: S001, S002, S003, S006, S008, S009, S010, S016, S018, S019, S020, S023, S026, S027, S028, S029, S032, S034, S035, S036, S040, S041, S044, S045, S046, S047, S050, S051, S058, S059
- `clip_audio`: S001, S007, S008, S010, S018, S019, S023, S024, S039, S060
- `bgm_mixed`: S003, S006, S009, S011, S015, S024, S025, S052, S056, S057, S059
- `unclear_audio`: S006, S012, S014, S015, S036, S039, S054
- `over_attributed_chunks`: S019, S049, S050, S054, S060

High-priority STT-related intersections:
- `multi_speaker`: S001, S002, S006, S008, S010, S016, S018, S019, S020, S023, S026, S027, S032, S040, S041, S044, S045, S046, S047, S050, S051, S059
- `clip_audio`: S001, S007, S008, S010, S018, S019, S023, S039, S060
- `over_attributed_chunks`: S019, S049, S050, S054, S060

## Interpretation

The current failure concentration points to the STT/source pipeline rather than
translation prompt quality as the first priority. The strongest evidence is:
- Most `clip_audio` cases are STT-related failures.
- Most `multi_speaker` cases are STT-related failures.
- Every `over_attributed_chunks` case is an STT-related failure.

The sample does not prove that Chxxnnx host-only STT is poor. Many failures came
from clip/other-streamer audio, overlapping speakers, or source chunks being
merged or attributed in a way that does not match the intended subtitle event.

## Recommended Next Work

1. Add a second-pass categorization for speaker source:
   - `host_only`
   - `clip_or_other_speaker`
   - `host_over_clip`
   - `multi_streamer`
   - `speaker_unclear`
   - `wrong_speaker_selected`

2. Analyze the 33 STT-related samples first, especially:
   - `clip_audio` + STT-related errors
   - `multi_speaker` + STT-related errors
   - all `over_attributed_chunks`

3. Before adding diarization or speaker separation, use the existing evidence to
   define the product policy:
   - translate Chxxnnx only,
   - translate the dominant audible speaker,
   - or translate mixed-stream content as heard.

4. Keep translation prompt/glossary work as a second priority. The 14
   translation-related errors are real, but they are not the dominant failure
   mode in this mixed-stream sample.
