# Speaker-Turn Safe Chunking Handoff

## Purpose

This document captures the current labeling evidence and working context for a
future `AGENTS.md` cross-review workflow. It is not an implementation plan.
Claude Code should inspect the code and produce the step-1 proposal from code,
runtime data, and this evidence.

## Source Files

First-round mixed-stream sample:
- `logs/labeling_sample_20260531_232759.json`
- `logs/labeling_sample_20260531_232759.annotations.json`

Second-round speaker/source subset:
- `logs/labeling_sample_20260531_232759.speaker_source_round2.json`
- `logs/labeling_sample_20260531_232759.speaker_source_round2.annotations.json`

Related local analysis:
- `LABELING_SAMPLE_20260531_ANALYSIS.md`

Note: local markdown files and logs may be git-ignored by repo-local ignore
rules. Verify file presence on disk, not only `git status`.

## User/Product Decisions

- Normal live subtitle behavior should translate the current primary audible
  speaker, not only Chxxnnx.
- The system should avoid merging multiple speakers into one subtitle event.
- Clean/non-overlapped audio appears to have acceptable translation quality.
- The next task should let Claude Code inspect the current pipeline and propose
  fixes. Do not pre-decide the implementation from this handoff.
- Do not start by integrating WhisperX, pyannote, diarization, voiceprint, or
  Chxxnnx-only speaker ID. These can be evaluated later if code/runtime evidence
  shows they are needed.

## First-Round Labels

Scope: 60 uniformly sampled mixed-stream translation events.

| Label | Count |
| --- | ---: |
| `b_stt_error` | 29 |
| `ok` | 13 |
| `a_translation_error` | 10 |
| `both` | 4 |
| `unclear` | 4 |

Derived counts:
- STT-related: 33 / 60 (`b_stt_error` + `both`)
- Translation-related: 14 / 60 (`a_translation_error` + `both`)
- Acceptable: 13 / 60 (`ok`)
- Unclear: 4 / 60

Context tags:

| Tag | Count |
| --- | ---: |
| `multi_speaker` | 30 |
| `bgm_mixed` | 11 |
| `clip_audio` | 10 |
| `unclear_audio` | 7 |
| `over_attributed_chunks` | 5 |

Important intersections observed earlier:
- `multi_speaker` + STT-related: 22 / 30
- `clip_audio` + STT-related: 9 / 10
- `over_attributed_chunks` + STT-related: 5 / 5

## Second-Round Speaker/Source Tags

Scope: 33 first-round STT-related samples. The second round is a rough source
classification pass, not word-level ground truth.

Completion:
- Entries: 33
- Speaker/source tagged: 33 / 33

Speaker/source tag counts:

| Tag | Count |
| --- | ---: |
| `host_over_clip` | 24 |
| `wrong_speaker_selected` | 12 |
| `speaker_unclear` | 11 |
| `audio_source_mismatch` | 8 |
| `multi_streamer` | 8 |
| `clip_or_other_speaker` | 6 |
| `host_only` | 2 |

Interpretation constraints:
- Tags are approximate. Treat them as directionally useful, not definitive.
- `host_over_clip` means Chxxnnx/host audio is present over clip/other-streamer
  audio; it does not necessarily mean Chxxnnx is the primary speaker.
- `wrong_speaker_selected` was used when the subtitle/source appeared to mix
  speakers or select a speaker contrary to the intended current-speaker turn.
- `audio_source_mismatch` was used when displayed source/evidence did not appear
  to match the attached audio chunks or event attribution.

Second-round mapping:

| Round ID | Original ID | Speaker/source tags |
| --- | --- | --- |
| R001 | S001 | `host_only`, `speaker_unclear` |
| R002 | S002 | `host_over_clip`, `wrong_speaker_selected` |
| R003 | S004 | `host_over_clip`, `wrong_speaker_selected`, `audio_source_mismatch` |
| R004 | S006 | `host_over_clip`, `speaker_unclear` |
| R005 | S007 | `audio_source_mismatch` |
| R006 | S008 | `host_over_clip`, `multi_streamer`, `wrong_speaker_selected` |
| R007 | S010 | `host_over_clip`, `multi_streamer`, `wrong_speaker_selected`, `audio_source_mismatch` |
| R008 | S016 | `host_over_clip`, `multi_streamer` |
| R009 | S018 | `clip_or_other_speaker`, `host_over_clip`, `multi_streamer`, `audio_source_mismatch` |
| R010 | S019 | `clip_or_other_speaker`, `host_over_clip`, `multi_streamer`, `speaker_unclear`, `wrong_speaker_selected` |
| R011 | S020 | `clip_or_other_speaker`, `audio_source_mismatch` |
| R012 | S021 | `clip_or_other_speaker` |
| R013 | S023 | `host_over_clip`, `multi_streamer` |
| R014 | S026 | `host_over_clip` |
| R015 | S027 | `host_over_clip`, `multi_streamer`, `speaker_unclear`, `wrong_speaker_selected` |
| R016 | S031 | `host_over_clip` |
| R017 | S032 | `host_over_clip` |
| R018 | S039 | `host_over_clip` |
| R019 | S040 | `host_over_clip`, `wrong_speaker_selected` |
| R020 | S041 | `host_over_clip`, `wrong_speaker_selected` |
| R021 | S042 | `host_only` |
| R022 | S044 | `clip_or_other_speaker` |
| R023 | S045 | `multi_streamer`, `speaker_unclear`, `wrong_speaker_selected` |
| R024 | S046 | `host_over_clip`, `speaker_unclear`, `wrong_speaker_selected` |
| R025 | S047 | `host_over_clip`, `speaker_unclear` |
| R026 | S049 | `host_over_clip` |
| R027 | S050 | `host_over_clip`, `wrong_speaker_selected` |
| R028 | S051 | `clip_or_other_speaker`, `host_over_clip`, `wrong_speaker_selected` |
| R029 | S052 | `audio_source_mismatch` |
| R030 | S053 | `speaker_unclear`, `audio_source_mismatch` |
| R031 | S054 | `host_over_clip`, `speaker_unclear` |
| R032 | S059 | `host_over_clip`, `speaker_unclear`, `audio_source_mismatch` |
| R033 | S060 | `host_over_clip`, `speaker_unclear` |

## Evidence-Based Observations

These are observations for reviewer verification, not final claims:

- Most first-round failures are STT/source-pipeline related, not pure translation
  failures.
- Most second-round STT-related samples are not clean host-only speech.
- `audio_source_mismatch` and `over_attributed_chunks` suggest at least some
  failures may be caused by attribution or chunk/evidence binding, not only STT
  model recognition quality.
- `host_over_clip`, `multi_streamer`, and `wrong_speaker_selected` suggest the
  pipeline may be merging multiple speaker turns into a single subtitle event.
- Translation quality appears acceptable when audio is not overlapped or
  attribution-mismatched, but this needs code/runtime validation rather than
  relying only on subjective labeling.

## Suggested Cross-Review Task Framing

Task title:
`speaker-turn safe chunking / attribution cleanup`

Ask Claude Code to produce a proposal only. It should inspect code before making
claims. It should not implement during the proposal step.

The proposal should verify:
- Where audio chunks and `utterance_id` are created.
- Where STT events are emitted.
- Where sentence/source utterance IDs are merged.
- Where translation events record attribution.
- How `primary`, `supporting`, and `prior_overlap` are reconstructed by the
  labeling sample scripts.
- Which code paths could explain `audio_source_mismatch` or
  `over_attributed_chunks`.

Non-goals for the proposal:
- No WhisperX or pyannote integration.
- No translation prompt/model/provider changes.
- No Chxxnnx-only voiceprint or speaker identity work.
- No full speaker diarization implementation.
- No broad refactor unless code evidence shows it is required.

Possible proposal directions to evaluate, without preselecting:
- Attribution cleanup only: repair event/source/audio binding and diagnostics.
- Speaker-turn guardrails: avoid merging high-risk chunks into one subtitle
  event.
- Conservative low-confidence behavior: shorten or suppress subtitles when
  evidence indicates speaker/source ambiguity.

Risks to evaluate:
- Lower subtitle coverage.
- Increased latency.
- Over-fragmented subtitles.
- Loss of useful context for translation.
- False positives on normal clean context merging.

Testing/validation areas to consider:
- Unit tests around source utterance ID propagation.
- Tests for sentence splitting/translation event attribution.
- Labeling sample generation tests for chunk role reconstruction.
- Collection sanity report diagnostics.
- Runtime event diagnostics that make future overlap/attribution failures easier
  to detect.

## Current Repo State Notes

As of this handoff, the repo has unrelated existing uncommitted changes from
prior work, including STT key-switching changes, labeling scripts/tests, and
legacy script removals. Do not revert or stage unrelated files unless explicitly
requested.

Recent committed fix:
- `05eef3d Keep pause indicator visible`
- Scope: pause indicator in `modules/subtitle_display.py` remains visible while
  pipeline is paused, with `tests/test_subtitle_display.py`.
