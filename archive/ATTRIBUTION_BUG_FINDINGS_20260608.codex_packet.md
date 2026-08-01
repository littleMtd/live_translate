# Source-Attribution Contamination — Evidence Packet for Independent Review

> You are reviewing a live-subtitle pipeline (Korean STT → sentence assembly →
> zh-TW translation). Below is observed evidence only. Diagnose independently.
> Do NOT assume any provided framing — there is none here on purpose.
>
> Answer two questions, citing code you read yourself:
>   Q1. What is the ROOT CAUSE of the attribution mismatch shown below? Quote the
>       exact code line(s).
>   Q2. Name the DEEPEST defect that a one-line fix would NOT solve. Explain why
>       the obvious one-line fix is insufficient.
>
> Then: list any defect you found that is NOT implied by the evidence below
> (i.e. something the evidence does not already point at).

---

## §1 Observed phenomena

### 1.1 Consecutive translations share entire source_utterance_ids

Run `20260606T095302Z-42260` (single Korean streamer, solo gaming),
translation events in order:

```
seq 47  natural        chunks=1  src=[utt-88]                      "음... 안녕히 계세요"
seq 48  forced_prefix  chunks=3  src=[utt-89,utt-90,utt-91]        "아티팩트를 얻을 수 있게 ... 최대 체력 증가."
seq 49  forced_blob    chunks=3  src=[utt-89,utt-90,utt-91]        "최대 체력"
                                  └ identical ids, no new chunk ┘

seq 162 natural        chunks=1  src=[utt-274]                     "아... 인걸블 한번 노려볼까?"
seq 163 forced_prefix  chunks=3  src=[utt-275,utt-276,utt-277]     "아아아아! ... 좋아 좋아 좋아"
seq 164 forced_blob    chunks=5  src=[utt-275,utt-276,utt-277,utt-278,utt-279]  "기왕에 하나 놓고..."
                                  └ inherited 275/276/277 ┘ └ new ┘

seq 392 natural        chunks=1  src=[utt-653]                     "아 이거 또 고인물들을..."
seq 393 forced_prefix  chunks=2  src=[utt-654,utt-655]            "고인물 컨텐츠죠?"
seq 394 forced_prefix  chunks=3  src=[utt-654,utt-655,utt-656]    "어 이이이이 ... 그곳은 너무 어두웠어."
                                  └ inherited 654/655 ┘ └ new ┘
```

### 1.2 Human listening (ground truth, during manual labeling)

Listening to each `utt-*.wav` for the forced cases above:
- The **new/primary** chunk matches the sentence text.
- The **inherited prior** chunks contain the **previous** sentence's speech, not
  the current sentence's text.
- The over-attribution is whole chunks, NOT the expected 0.4–1.0 s VAD audio
  overlap.

### 1.3 cut_reason distribution (same run, 412 translations)

| cut_reason | count | % |
|---|---|---|
| natural | 217 | 53% |
| forced_prefix | 137 | 33% |
| forced_blob | 44 | 11% |
| merged:* | 14 | 3% |
| forced=True total | 195 | 47% |
| chunk_count > 1 | 198 | 48% |

### 1.4 Labeled error distribution split by cut type (60 human-labeled samples, a different earlier run)

Join: sample `utterance_id` → `sentence` event `cut_reason`.
Labels: a_translation_error / b_stt_error / ok / both / unclear.

| label | natural (34) | forced (26) |
|---|---|---|
| b_stt_error | 41% | 58% |
| ok | 32% | 8% |
| a_translation_error | 15% | 19% |
| both / unclear | 12% | 16% |

Of the 29 `b_stt_error` samples: the 5 human-tagged `over_attributed_chunks` are
all `b_stt_error`; 7/29 carried a source id seen in a prior translation;
`a_translation_error` had 0 such overlap.

### 1.5 Narrowest contamination measure

Translations sharing ≥1 source id with the immediately-preceding translation:
11/412 (3%) in this run; 10/60 in the labeled sample.

---

## §2 Where to look

- `modules/sentence_buffer.py` — `pop_ready()`, the `forced` branch
  (`forced_prefix` residual handling) and the `SentenceCut` tally fields.
- `modules/sentence_splitter.py` — `merge_cuts()`.
- `modules/pipeline_events.py` — `source_utterance_ids` on `SentenceEvent` and
  `sentence_metadata()`.
- `modules/audio_capture.py` — VAD chunking + `vad_overlap_sec`.
- `scripts/sample_labeling_cases.py` — how `source_chunks[]` /
  `source_chunk_usages[]` and prior-overlap are reconstructed offline.
- Git: `git show 3b45025 -- modules/sentence_buffer.py` is relevant history.

## §3 Impact context (factual)

- Clean 1:1 (attributed-audio ↔ sentence-text) appears to hold only for natural
  cuts (53%). The 47% forced cuts attribute a multi-chunk audio span to a partial
  text.
- The labeling method `source_utterance_ids → play audio → judge STT` is used to
  classify each subtitle as translation-error vs STT-mishearing. If attribution is
  wrong, that classification is wrong.

---

Reminder: answer Q1 (root cause + code line), Q2 (deepest defect a one-line fix
won't solve + why), and list any defect the evidence above does NOT already
point at.
