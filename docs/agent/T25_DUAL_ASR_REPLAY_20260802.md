# T25 dual-ASR replay — 2026-08-02

## Decision and scope

The owner explicitly chose to keep two local ASR candidates. SenseVoice and
faster-whisper are therefore independent evidence sources; maintenance cost is
not an elimination criterion. This replay does not replace Groq, change the
live STT route, or authorize any prompt, glossary, profile, correction,
quality-gate, routing, or deadline change.

The owner also prefers updating dependencies over permanently pinning a local
ASR environment. The versions below identify this evidence run, not a new
production lock file. A future dependency/model update must record its versions
and create a new result artifact; it must not silently overwrite this baseline
or be compared as if settings were identical.

## Frozen inputs and outputs

- Manifest: `data/t25_stt_replay_manifest_20260802.json`.
- SenseVoice result: `data/t25_sensevoice_shadow_20260802.json`.
- faster-whisper result: `data/t25_faster_whisper_shadow_20260802.json`.
- Cases: 21 annotations carrying `audio_replay_required`.
- Audio: 31 current translation-source WAVs plus three context-evidence WAVs,
  34 unique assets, 223.688 seconds, 7,159,512 bytes.
- Comparison contract: only the 31 `source_kind=current` assets are aggregated
  into candidate text. The three evidence assets remain separately visible.
- Annotation 68 remains outside the 21 replay cases because it was not labeled
  `audio_replay_required`, not because audio provenance is missing. Schema v2
  of the separate provenance manifest now retains its pure-residual evidence
  IDs `utt-26,utt-27`. This rebuild does not change this replay manifest or
  either preserved ASR result.

The manifest SHA-256 is
`6d4afaf8ac1d3953f2b03bf27ef4a1f96f0148a905e6f2fd7394d21ae4261745`.
The preserved result-file SHA-256 values are
`4fe561c39d856e52356f4321ac7b6dc27a84528f878fd832a1d43740bbe90d13`
(SenseVoice) and
`a800458355eb0780f9befea5c2d6ab1bb5e9a599f6fb606e0765aef43ddc6ff3`
(faster-whisper).

## Runtime configuration

The disposable environment is outside the repository at
`C:\Users\user\.cache\live_translate\asr-venv`; model caches are under
`C:\Users\user\.cache\live_translate\models`. No project requirement file or
`live-subtitle-env` package was changed for this experiment.

Observed packages were Python 3.13.14, FunASR 1.4.0, faster-whisper 1.2.1,
CTranslate2 4.8.1, PyTorch 2.13.0+cpu, torchaudio 2.11.0+cpu, NumPy 2.4.6, and
SoundFile 0.14.0. The torch/torchaudio version skew is a non-production risk;
imports and the full NumPy-input SenseVoice replay succeeded, but this is not a
claim of general torchaudio compatibility.

- SenseVoice: `iic/SenseVoiceSmall`, CPU, Korean, ITN on, batch size 60
  seconds. ModelScope exposed only the moving `master` snapshot; the downloaded
  `model.pt` SHA-256 is
  `833ca2dcfdf8ec91bd4f31cfac36d6124e0c459074d5e909aec9cabe6204a3ea`.
- faster-whisper: `large-v3-turbo`, CPU int8, six CPU threads, one worker,
  Korean, beam 5, VAD off, previous-text conditioning off, word timestamps off.
  Hugging Face resolved snapshot
  `0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf`; `model.bin` SHA-256 is
  `e76620f83d5f5b69efd3d87e3dc180c1bd21df9fbebacfd4335e5e1efcc018da`.
- faster-whisper passed a real `HF_HUB_OFFLINE=1` plus
  `--local-files-only` smoke replay after download.

## Mechanical results

These timings cover per-chunk inference after model construction; first-run
download and model-load time is excluded. The current assets contain 200.920
seconds of audio. The reported p95 is the observed lower-index percentile at
`int(0.95 * (n - 1))`; it is not an interpolated or nearest-rank percentile.

| Engine | Current cases/chunks | Empty cases | Inference total | Median/chunk | p95/chunk | RTF |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| SenseVoice | 21 / 31 | 0 | 9.296 s | 280.4 ms | 457.0 ms | 0.046 |
| faster-whisper | 21 / 31 | 0 | 196.002 s | 5,473.5 ms | 6,217.2 ms | 0.976 |

Speed is descriptive only. The owner decision keeps both engines, so the table
does not select a winner.

## Case-level evidence review

The table records observable agreement and disagreement. “Supports” means the
audio-derived candidate strengthens a hypothesis; it is not a ground-truth
label and does not authorize a deterministic rewrite.

| Case | Observed dual-ASR signal | Evidence consequence |
| --- | --- | --- |
| T25-049 | faster-whisper retains logged `도미선양`; SenseVoice emits `도선냥`; ice-cream context separately favors `더위사냥` | confirms that engine agreement with Groq is not canonical-entity proof; exact product word still needs listening |
| T25-053 | both emit an `엉걸`-like form rather than logged/human `언걸` | supports a Groq spelling concern; exact entity remains unresolved |
| T25-055 | faster-whisper exactly emits `혁미경`; SenseVoice emits `형미경`; eye/camera context separately favors `현미경` | independent ASR reproduces Groq's token but does not resolve the contextual near-homophone |
| T25-056 | both current-only candidates begin after the logged source start; the preceding phrase appears in evidence WAV `utt-94` | confirms the intentional carry-forward contract: current IDs cover new audio and `utt-94` is preserved separately as evidence; this is not lost live provenance; `꺼드렁` remains unresolved |
| T25-057 | both broadly retain `제노드란`, `호술할`, and `제트래곤` variants | no safe exact correction established |
| T25-061 | faster-whisper emits `자개`, SenseVoice `자게`, Groq `자괴` | supports a Groq mishear hypothesis; faster-whisper supplies the leading candidate |
| T25-062 | both retain `그게 풀 그거요?`, while neighboring gacha/duplicate context favors a `풀돌` reading | dual-ASR agreement does not resolve the contextually supported game term |
| T25-063 | faster-whisper retains `명조코들`; SenseVoice splits `명조 코드들` | favors faster-whisper for the entity boundary, not the full sentence meaning |
| T25-067 | faster-whisper retains `징거부가`; SenseVoice emits `증거부가` | faster-whisper corroborates Groq's token |
| T25-070 | SenseVoice emits a `평생`-like phrase; faster-whisper retains a `표세`-like form; evidence WAV supplies the preceding mother/nearby-home context | context makes `평생` plausible, but the engines do not agree |
| T25-073 | faster-whisper closely tracks Groq; SenseVoice has a small early corruption; idiomatic context separately favors `몸소` | the engines do not corroborate the human candidate, so the token remains a listening decision |
| T25-074 | faster-whisper emits `어떤 운을 타고난 걸까`; SenseVoice emits `어떤 눈을 타고난 걸까`; both reject Groq `어떤 오늘 타고 날까` | strong evidence of a Groq error; `운` is the stronger contextual candidate |
| T25-078 | faster-whisper emits `저 정도야?`; SenseVoice emits `조 정도야` | supports `저 정도야? 괜찮아?` as the leading candidate |
| T25-081 | both expose unstable `비찬/비차`, `모험/모음`, and capture-term boundaries; faster-whisper preserves more named tokens | confirms entity/segmentation instability, not one global normalization |
| T25-083 | both disagree with Groq around `섭취/석주/썩주` and wing-item forms | STT issue supported; exact words remain unresolved |
| T25-084 | both current-only candidates omit logged `시청자`; remaining `롤러/로러/요료` differs | one current chunk and no evidence IDs falsify carry-forward for this case; Groq/local-ASR disagreement and the spoken subject remain unresolved |
| T25-086 | both preserve `르르` but disagree in the following clothing phrase | partial entity corroboration only |
| T25-087 | faster-whisper retains logged `쇼똥부` and `초록돌기님`; SenseVoice corrupts both; drawing-team context permits alternatives such as `쇼츠부` | faster-whisper corroborates the logged spellings only, not canonical identities |
| T25-090 | both broadly agree with the logged sentence, while neighboring lines repeatedly discuss `성능 차이` | dual ASR does not corroborate the proposed correction; contextual listening remains necessary |
| T25-091 | faster-whisper clearly retains `부가땅 절찌`; SenseVoice emits `부가 땅 절제` | strong faster-whisper corroboration; the visual/entity check still remains separate |
| T25-092 | both independently emit `어차피 스토리 밀다 보면 얘 뽑을 수 있어요`, unlike the logged first clause | strong Groq source/segmentation suspicion; later clauses broadly agree |

## What this resolves and what it does not

The dual replay supplies new non-manual prioritization evidence. T25-074,
T25-078, T25-091, and T25-092 now have especially useful alternate candidates;
T25-056 demonstrates why current and evidence text must be interpreted
together; T25-084 is not a carry-forward case. T25-049,
T25-055, T25-062, T25-073, T25-087, and T25-090 also demonstrate why two-ASR
agreement or reproduction of Groq text cannot override contextual evidence.
This reduces the amount of listening needed to a small disagreement/decision
slice instead of another broad annotation pass.

It does not create reference transcripts, WER/CER scores, matched OK controls,
or proof that one engine is semantically correct. Exact production changes
still require a bounded assertion plus a control or a short audio judgment for
the specific disputed token. The ASR outputs themselves must never be promoted
automatically into source normalization or glossary rules.

The focused source-attribution follow-up is recorded in
`docs/agent/T25_ATTRIBUTION_AUDIT_20260802.md`. It falsifies a shared
T25-056/T25-084 attribution defect and identifies a separate offline
annotation-68 manifest omission. That omission is repaired by schema v2 of
`data/semantic_quality_evidence_20260802.json`; this replay baseline remains
byte-identical.

## Reproduction commands

Run the engines separately so failure and memory use remain isolated:

```powershell
$asrPython = 'C:\Users\user\.cache\live_translate\asr-venv\Scripts\python.exe'
& $asrPython scripts\replay_phase0_stt_candidates.py `
  --engine sensevoice `
  --manifest data\t25_stt_replay_manifest_20260802.json `
  --group audio_replay_required --device cpu `
  --output scratch\analysis\t25_sensevoice_shadow_latest.json

& $asrPython scripts\replay_phase0_stt_candidates.py `
  --engine faster-whisper `
  --manifest data\t25_stt_replay_manifest_20260802.json `
  --group audio_replay_required --device cpu --compute-type int8 `
  --cpu-threads 6 --num-workers 1 `
  --download-root C:\Users\user\.cache\live_translate\models\faster-whisper `
  --local-files-only `
  --output scratch\analysis\t25_faster_whisper_shadow_latest.json
```
