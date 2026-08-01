# Source-Attribution Contamination — Findings (2026-06-08)

> LOCAL ONLY — do not commit/push (analysis doc, same policy as OPTIMIZATION_*.md).
> Purpose: hand §1–§3 (evidence) to Codex for an INDEPENDENT diagnosis, then
> compare against §4 (Claude's diagnosis) to test whether both passes reach the
> same root cause, or whether one stops at the surface mechanism.

## How to use this for cross-review (low framing bias)

1. Give Codex **§1–§3 only** (observed phenomena + reproduction + impact).
   Do NOT show §4 yet.
2. Ask Codex two evidence-forced questions, blind to Claude's reasoning:
   - "What is the root cause of the attribution mismatch? Quote the code."
   - "Name the deepest defect that a one-line fix would NOT solve."
3. Then reveal §4 and diff the two diagnoses. The point of interest is the
   *deepest* defect each side names, not the surface mechanism (both will
   likely find that).

---

## §1 Observed phenomena (evidence, no conclusions)

### 1.1 Consecutive translations share entire source_utterance_ids

Run `20260606T095302Z-42260` (isegye_lilpa, solo gaming), translation events:

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

### 1.2 Human listening (ground truth, during labeling)

For the forced cases above, listening to each `utt-*.wav`:
- The **new/primary** chunk matches the sentence text.
- The **inherited prior** chunks contain the **previous sentence's** speech, not
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
| **forced=True total** | **195** | **47%** |
| chunk_count > 1 | 198 | 48% |

### 1.4 Labeled error distribution split by cut type (round-0, hades, 60 samples)

Join: sample `utterance_id` → `sentence` event `cut_reason` (run `20260531T115809Z-123124`).

| label | natural (34) | forced (26) |
|---|---|---|
| b_stt_error | 41% | **58%** |
| ok | **32%** | 8% |
| a_translation_error | 15% | 19% |
| both / unclear | 12% | 16% |

Also, of the 29 `b_stt_error` samples, the 5 human-tagged `over_attributed_chunks`
are **all** `b_stt_error`; 7/29 carried `source_overlap_warning` (a source id seen
in a prior translation). `a_translation_error`: 0 overlap.

### 1.5 Scale of the narrow (id-reuse) subset

Translations sharing ≥1 source id with the immediately-preceding translation:
**11/412 (3%)** in the 0606 run; **10/60** in the round-0 sample. (This is the
*narrowest* contamination measure — see §3 for why it understates.)

---

## §2 Reproduction

Single-run sanity (audio join + diagnostics):
```
live-subtitle-env\Scripts\python.exe scripts\collection_sanity_report.py \
  --events logs\runtime_events_20260606.jsonl --run-id 20260606T095302Z-42260
```

The specific sequences in §1.1 / cut distribution in §1.3 are read directly from
`logs/runtime_events_20260606.jsonl` (filter `event_type=="translation"`,
`run_id=="20260606T095302Z-42260"`). The cut-split in §1.4 joins the round-0
sample/annotations against `sentence` events in `logs/runtime_events_20260531.jsonl`.

Relevant code: `modules/sentence_buffer.py` `pop_ready()` forced branch
(`forced_prefix` residual handling) and `merge_cuts()` in
`modules/sentence_splitter.py`. Attribution surfaced via
`source_utterance_ids` in `pipeline_events.py` → `sentence_metadata()` →
translation event, and reconstructed offline in
`scripts/sample_labeling_cases.py` (`source_chunks[]` / `source_chunk_usages[]`).

---

## §3 Quantified impact

- The clean 1:1 (attributed-audio ↔ sentence-text) relationship holds **only for
  the 53% natural cuts**.
- The remaining **47% forced cuts** attribute a full multi-chunk audio span to a
  partial text:
  - `forced_prefix`: text = the **head** of the chunk span; the residual tail of
    the same chunks is carried forward → listener hears "text + trailing extra".
  - `forced_blob` (residual side, e.g. seq 49 / 164): attributed chunks are
    largely the **previous** sentence's audio → listener hears the wrong content.
- Consequence A (diagnostic): the `source_utterance_ids → audio → judge STT`
  method is unreliable on ~47% of data. The headline "b_stt_error dominant
  (≈48–55%)" is partly an artifact: restricting to clean natural cuts drops
  b_stt_error 58%→41% and raises ok 8%→32% (§1.4). (Confounded: forced cuts are
  *also* genuinely harder audio, so not all of the gap is the bug.)
- Consequence B (subtitle, user-visible): seq 48 "…최대 체력 증가." followed by
  seq 49 "최대 체력" is a duplicated residual fragment shown as its own line.

---

## §4 [SEALED — reveal only after Codex's independent pass] Claude's diagnosis

### 4.1 Surface mechanism

`modules/sentence_buffer.py`, `forced_prefix` residual branch: when a significant
residual is carried back into the buffer, only the time-box clock is reset.
`_source_utterance_ids`, `_chunk_count`, `_total_audio_seconds`,
`_source_avg_logprobs`, `_source_no_speech_probs` are **not** reset, so the
residual-derived next sentence inherits the prior chunks' full tally.

Introduced deliberately by commit `3b45025` ("Fix forced-prefix residual dropping
source attribution"), which flipped the behavior from **zero-out (drops sources)**
to **carry-forward (over-counts sources)**. Both are wrong in opposite directions.

### 4.2 Why a one-line "reset the tallies" fix is NOT enough

The commit message itself states the unresolved core: *"a single STT chunk can
straddle the punctuation boundary, so the residual may derive from any chunk seen
so far and the sources cannot be cleanly partitioned."* Resetting tallies just
returns to the old zero-out bug (under-attribution). The two known options are a
false dilemma — both lose information.

### 4.3 Candidate deeper defects (the actual question for cross-review)

- **D1 — No text→chunk offset map.** `SentenceBuffer` holds a flat
  `_source_utterance_ids` list and a single concatenated `_buffer` string, with no
  record of which character range came from which chunk. Without that, prefix vs
  residual can never be attributed correctly — both zero-out and carry-forward are
  guesses. This is the root; the carry/zero debate is a symptom.
- **D2 — Forced-cut family is mis-attributed even with no residual reuse.** Every
  `forced_prefix` (33%) attributes the full chunk span to a head-only text,
  independent of the seq-N+1 inheritance. So the contamination surface is ~47%
  (forced), not the 3% id-reuse subset. A fix targeting only id-reuse misses most
  of it.
- **D3 — Chunk boundaries ⟂ sentence boundaries by construction.** VAD cuts chunks
  on silence/time; the buffer cuts sentences on punctuation. They never align, so
  chunk-level attribution is inherently fuzzy for any multi-chunk sentence (48%).
  Clean attribution may require word/segment-level timestamps from STT, not just
  chunk ids — a larger scope than sentence_buffer.
- **D4 — VAD audio overlap is a second, independent contamination.**
  `vad_overlap_sec` makes adjacent chunk WAVs physically share 0.4–1.0 s of audio.
  Even a correctly-attributed natural cut's chunk audio contains neighbor bleed.
  Orthogonal to the source-id bug; a source-id fix does not touch it.
- **D5 — The 47% forced rate is the upstream disease.** Attribution contamination
  scales with forced-cut frequency (round-0 hades 17% id-reuse vs 0606 solo 3%,
  tracking how choppy the speech is). Fixing attribution makes the data usable but
  does not reduce *why* half of all speech fails to get a clean punctuation
  boundary inside the time window.

### 4.4 What Claude is NOT claiming

- Not claiming the 58%→41% b-rate drop is entirely the bug (D-confound: forced =
  harder audio too).
- Not claiming a fix is scoped/approved. This doc is diagnosis only; any fix goes
  through the normal plan → Codex cross-review → sign-off workflow.

---

## §5 Open questions for cross-review (stakes-layered)

High stakes (wrong answer ships a bad fix):
- Q1. Is D1 (no text→chunk map) the true root, or is there a correct chunk-level
  partition that does not need offsets? Quote code.
- Q2. Does any current consumer **rely** on the over-counting behavior (e.g.
  `source_chunk_usages[]` prior-overlap detection, or merge_cuts)? Would fixing
  attribution break a test or a diagnostic that was calibrated on the buggy data?

Medium:
- Q3. Is D4 (VAD overlap) actually present at labeling-relevant magnitude in the
  dumped WAVs, or negligible?
- Q4. Should the fix live in `sentence_buffer` (attribution) or upstream (reduce
  forced rate, D5), or both — and in what order?

Low:
- Q5. Is the seq-49-style duplicate-residual subtitle (Consequence B) worth a
  separate dedup, or does fixing attribution incidentally resolve it?
