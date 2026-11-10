# T25 runtime/audio evidence intake — 2026-08-02

## Scope

This intake preserves the owner's 95 annotations from
`live_translate_QA_merged_20260802.json` and links them to the four effective
live runs from the same date. The intake itself is provenance evidence, not a
production change or a ground-truth set; a separately reviewed 16-case bounded
evaluator is listed below.

- Lossless source: `data/manual_quality_annotations_20260802.json`.
- Provenance manifest: `data/semantic_quality_evidence_20260802.json`.
- Bounded evaluator: `data/semantic_quality_eval_20260802.json`.
- Frozen replay manifest/results: `data/t25_stt_replay_manifest_20260802.json`,
  `data/t25_sensevoice_shadow_20260802.json`, and
  `data/t25_faster_whisper_shadow_20260802.json`.
- Runtime source: `logs/runtime_events_20260802.jsonl`.
- Audio source: `logs/audio_dump/<run_id>/<utterance_id>.wav`.

The raw file contains 95 unique `sequence_id` values (1–95), with 86
`bad` and nine `warn` labels. Parsed key/value equality against the supplied
attachment is the preservation gate. Suggested translations and rules remain
human proposals in the raw file; the manifest does not import either as an
expected output or a confirmed root cause.

## Runtime and audio inventory

| Run | Active profile | Translation events | Successful STT | WAV files |
| --- | --- | ---: | ---: | ---: |
| `20260802T084530Z-30796` | `hades_chxxnnx` | 61 (60 success, 1 filtered) | 74 | 74 |
| `20260802T090406Z-7548` | `url` | 193 success | 262 | 262 |
| `20260802T093732Z-30160` | `isegye_lilpa` | 264 (258 success, 6 filtered) | 309 | 309 |
| `20260802T103625Z-29500` | `url` | 9 success | 12 | 12 |

The four effective runs contain 527 translation events and 657 successful STT
events/WAV files. Successful STT utterance IDs and WAV filenames match
one-for-one. This verifies retained audio for successful STT calls; it does not
cover filtered or skipped STT candidates because the current dump path does not
write WAVs for those outcomes. Run `20260802T090356Z-13668` was startup-only
and contributes no quality evidence.

The first singing run used `hades_chxxnnx`, not `url`. Therefore annotation
2's observed `모카` miss cannot be attributed to a failed URL-profile rule
from this runtime alone. This is a concrete example of why the supplied
`rule_target` is triage metadata rather than verified ownership.

## Timestamp and provenance contract

The annotations contain 109 displayed timestamp tokens. Joining only
translation events in the same Asia/Taipei display second produces 117
candidates:

- 101 timestamps have one candidate;
- six multi-candidate timestamps are selected by normalized source/target text
  similarity with a frozen threshold (top score at least 0.30 and margin at
  least 0.10);
- two additional multi-candidate timestamps are selected by distinctive token
  containment: annotation 83 selects sequence 105 and annotation 91 selects
  sequence 216;
- no same-second annotation remains ambiguous.

Second-granularity timestamps are never described as exact event identity.
Every manifest reference carries `(run_id, translation_sequence_id)`, the UTC
runtime timestamp, local annotation time, source/target text, similarity
scores, selection status, source utterance IDs, STT join status, WAV path, and
WAV existence status. For range annotations, each displayed endpoint is joined
independently; intervening subtitles are not silently imported.

The annotation-linked offline manifest gap is resolved in schema v2:

- annotation 68 selects run `20260802T090406Z-7548`, translation sequence 20.
  The pure-residual live event intentionally has no current
  `source_utterance_ids` but does have
  `evidence_source_utterance_ids=["utt-26","utt-27"]`. The rebuilt provenance
  manifest now retains both IDs as `source_kind=evidence`, joins their unique
  successful STT rows and existing WAVs, and reports
  `runtime_translation_candidates_linked`. The selected missing-provenance
  count is now zero;

The rebuild preserves all frozen timestamp candidates, scores, and selection
statuses. Across 117 candidate runtime refs it records 152 current and 27
evidence audio refs. Every ref has an explicit evidence-ID list and every audio
ref has `source_kind`. Validation is:

```powershell
.\live-subtitle-env\Scripts\python.exe `
  scripts\rebuild_semantic_quality_provenance.py --check
```

Annotation 83's same-second sequence 104 has no source IDs, but distinctive
annotation tokens occur only in sequence 105, so sequence 104 is explicitly
`not_selected` rather than an annotation-linked gap. Annotation 91 similarly
selects sequence 216; sequence 215/`utt-273` is retained only as a separate
context-evidence asset in the replay manifest.

A third run-level missing-ID event exists at run
`20260802T093732Z-30160`, sequence 26 (17:43:32), but it is not directly
linked to annotation 49 at 17:43:33. The annotation's same-second sequence 27
has joined utterance IDs, so the third event is retained only as neighboring
run evidence.

## Label state and next gate

The manifest uses multi-value `label_states` because one annotation may
contain a safe bounded assertion and an unresolved audio/context claim at the
same time:

- 24 candidate assertions, including eight partial assertions;
- 21 audio-replay-required cases;
- five context-human-review cases;
- two entity/visual-confirmation cases;
- one cross-case observation (annotation 19);
- 46 cases pending independent semantic review.

These counts intentionally overlap. Annotation 19 summarizes a Palworld
glossary span and must not be independently weighted as another atomic failure.
Some suggested references add facts not present in the source, so none of the
95 suggested translations is ground truth merely because it was supplied by a
human.

Sixteen reviewed bounded contrasts are now executable: their references pass
16/16 and their captured outputs fail 16/16. Offline replay is also complete for
all 21 marked cases with both SenseVoice and faster-whisper: 31 current WAVs and
three separately reported evidence WAVs produced no empty case output. See
`docs/agent/T25_DUAL_ASR_REPLAY_20260802.md` for configuration, timings,
case-level disagreement, and limitations.

The next gate is a short token-level listening/visual check only for decisions
that would change production, plus matched controls for the chosen owning
layer. No production prompt, glossary, source normalization, target
replacement, profile, quality gate, route, or deadline change is authorized by
this intake or by dual-ASR agreement alone.
