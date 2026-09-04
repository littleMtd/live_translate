# T25 Weak-Signal Dual-ASR Evidence (2026-08-03)

## Decision and scope

This is an offline, no-label evidence pass over the four effective runs in
`data/semantic_quality_evidence_20260802.json`. It does not change live STT,
context eligibility, VAD, prompts, translation output, or routing. The result
remains **live no-go**: the cohort is small, matched controls are not
known-correct, and engine agreement is not ground truth.

The owner previously chose to retain both local ASR engines and to allow their
packages/models to update. Evidence runs therefore record package/runtime
versions and frozen input/output hashes; this experiment does not permanently
pin either external environment.

## Frozen cohort

The input population is exactly 657 successful, live, Groq
`whisper-large-v3` STT events with readable retained WAVs in the four T25 run
summaries. The weak cohort requires:

- `-1.0 <= avg_logprob < -0.7`;
- raw `audio_rms < 0.01`;
- `no_speech_prob <= 0.3`;
- `context_included=true`;
- 16 kHz mono PCM16 WAV with duration matching telemetry.

This yields ten weak chunks: two from `20260802T084530Z-30796` and eight from
`20260802T093732Z-30160`. T25-092 / `utt-281` is included. Each control is an
unannotated high-confidence candidate (`avg_logprob >= -0.3`) matched without
replacement on run, profile, model, VAD cut reason, overlap class, and Groq
structural-comparability status. Duration delta is at most 1.5 seconds and raw
RMS delta at most 0.006. All ten weak cases found a control within the frozen
calipers; failure to match would have stopped manifest construction.

Groq comparison text is admitted only for a successful single-source,
no-evidence translation whose raw source length matches the STT event's
recorded text length. Six weak/control pairs satisfy that rule. Four pairs,
including T25-092, remain two-local-engine comparisons and do not invent a
per-WAV Groq transcript from an assembled sentence.

## Replay results

Both engines returned non-empty output for all 20 chunks (103.744 seconds of
audio). The fixed comparison uses Unicode NFKC, case folding, and Unicode
alphanumeric characters. The diagnostic `local_consensus_disagreement`
requires local/local similarity at least 0.8 and both local/Groq similarities
at most 0.5; empty output is non-comparable and excluded from the denominator.

| Cohort | Cases | Three-way comparable | Local similarity median | Exact local match | Consensus disagreement |
|---|---:|---:|---:|---:|---:|
| weak signal | 10 | 6 | 0.525 | 0 | 0/6 |
| matched control candidate | 10 | 6 | 0.899 | 2 | 0/6 |

The weak cohort has materially lower local/local agreement than its matched
controls, but this is a descriptive association, not an error-rate estimate.
Most importantly, the frozen strict signal found no case where both local
engines strongly agreed with each other while both strongly disagreed with a
comparison-safe Groq string. This pass therefore does not support a live
secondary-ASR rule or a context-threshold change.

T25-092 remains the strongest non-comparable local agreement: SenseVoice and
faster-whisper have similarity 0.939 and both begin with the same
`어차피 스토리 밀다 보면 ...` reading. This prioritizes that one optional
blind listen; it still does not establish the exact heard source or prove that
prompt context caused Groq's assembled sentence.

## T25-092 blinded adjudication

The owner subsequently completed a single-WAV blind transcription in the
existing local labeling UI. Source, target, model, and all engine candidates
were hidden. The saved `ok` transcription for `utt-281` is:

`어차피 스토리 밀다보면 얘 뽑을 수 있어요? 질문하시면 지금 못 뽑아요 답변 올거에요`

After NFKC/alphanumeric normalization it has similarity 0.971 to the frozen
faster-whisper result and 0.939 to SenseVoice. This independently confirms the
local engines' shared `어차피 스토리 밀다보면 ...` reading for this WAV and
contradicts the content of the logged assembled Groq source beginning
`시청자님께서는 ...`. It does not reconstruct a persisted per-WAV Groq
transcript, adjudicate the separate overlapping `utt-282`, prove that prompt
context caused the Groq result, or generalize beyond this one case. The note
`只有訂閱聲音` is preserved verbatim; no speaker-source tag was selected, so
speaker ownership is not inferred.

T25-092 is therefore promoted from audio-pending to exact owner-heard evidence
for `utt-281` only. This still does not authorize a confidence-threshold,
context, or live dual-ASR change.

Two very weak chunks triggered a precision-oriented repetition proxy in
faster-whisper. `utt-307` is a different failure shape: faster-whisper equals
the comparison-safe Groq `감사합니다`, while SenseVoice emits a long unrelated
sentence. These observations argue against treating either local engine as an
automatic oracle.

## Determinism and performance

An initial faster-whisper run inherited its default temperature fallback and
changed outputs across repeated decoding of the weakest clips, including a
long repeated-token output. The replay tool now explicitly records and uses
`temperature=0.0`. A three-case repeat (`pair-001`, `pair-003`, and T25-092)
then matched the canonical faster-whisper output exactly in all three cases.
The deterministic outputs still contain repetition on the first two clips;
determinism does not make them correct.

| Engine | Package/runtime | Inference | RTF | Median chunk latency |
|---|---|---:|---:|---:|
| SenseVoiceSmall | FunASR 1.4.0 / torch 2.13.0+cpu | 5.079 s | 0.049 | 263.2 ms |
| large-v3-turbo | faster-whisper 1.2.1 / CTranslate2 4.8.1 | 105.409 s | 1.016 | 5,242.1 ms |

Both ran under Python 3.13.14 with NumPy 2.4.6 and SoundFile 0.14.0. The first
SenseVoice run downloaded the current ModelScope `master` artifact; the engine
does not expose a uniform immutable upstream revision, so the replay records
the package/model name and explicitly retains that identity limitation.

## Preserved artifacts

- `data/t25_weak_signal_manifest_20260803.json` —
  `dc5cc5a045829a76debafbcc26f1494c701e6fe05dc38dc251d522bccf944ae4`
- `data/t25_weak_signal_sensevoice_20260803.json` —
  `422bd1dd074f36af040c4ece6b5c482818be477481475a3428b06e58109a6f2f`
- `data/t25_weak_signal_faster_whisper_20260803.json` —
  `15073f4e5834e770e537e20f66830a56e1b4f3f1164e14ad4a7200930b6a19df`
- `data/t25_weak_signal_faster_whisper_repeat_20260803.json` —
  `afa4834bf82e9cbb7d96286fe428996fb79c296bd40dcbe0ff7b07516899eb17`
- `data/t25_weak_signal_dual_asr_20260803.json` —
  `c5618c1fa682030b06138f1f51a9410610ac28765bbb60436ffefee326161a00`
- `data/t25_092_blind_listen_20260803.json` —
  `cf6318ffcb55e065fd8b4ee9f94c48a319fb32538d6c3ed5e0972a8bc6dfaadb`
- `data/t25_092_blind_listen_annotations_20260803.json` —
  `627a7171d660698495d29a436df199b928492e62cbf7a184990f36c91ea083b5`

The artifacts were executed under `scratch/analysis` and promoted unchanged;
their recorded source paths intentionally preserve the actual execution
location. WAV fingerprints remain the authoritative input identity.

## Stop condition and next evidence

This card stops without production implementation. No broad manual labeling is
needed. T25-092's requested blind listen is complete; do not expand it into a
batch from this one confirmed case. Do not use the ten matched controls as
regression sign-off until the specific controls needed by a later change are
independently accepted.
