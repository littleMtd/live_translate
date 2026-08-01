# 架構改造提案：Audio-Grounded Evidence Pipeline

日期：2026-06-13
作者：Codex

## 結論

目前架構的品質上限主要卡在「單一路徑文字 STT -> 文字翻譯」。

繼續做 code review、prompt 微調、translation label 修補，仍然有價值，但邊際收益會越來越低。原因是翻譯模型看到的 source text 很多時候已經是錯的，後段 translator 無法穩定還原原始語音。

下一代架構應該從 translator pipeline 改成 evidence pipeline：

```text
audio chunk
  -> STT candidate generator
  -> TranslationEvidence
  -> transcript resolver
  -> translation candidate generator
  -> subtitle policy
  -> QA/event log
```

核心方向是：

```text
multi-STT candidates + transcript resolver + low-confidence audio fallback
```

## 對 Claude Code 方案的判斷

Claude Code 提到的方向大致可分為：

- A1：直接 audio-to-translation
- A2：STT N-best / 多 STT 候選
- B1：draft/refine 雙階段字幕
- C1：更好的 context memory
- C2：post-hoc QA / correction loop

建議排序：

1. 先做 A2：多 STT 候選 + resolver
2. 再做 A1-lite：只在低信心片段用 audio model 查證
3. 最後做 B1：live draft + delayed correction
4. C1/C2 當輔助，不當主架構

理由：

- 最大錯誤源常常不是 translator，而是 STT 已把語音壓成錯文字。
- audio-to-translation 全量啟用成本和 latency 高，應該先作為低信心 fallback。
- draft/refine 會改 UI 與事件語義，工程風險較高，應在 evidence pipeline 穩定後再做。

## 目標架構

目前：

```text
audio -> STT single text -> sentence splitter -> translator -> subtitle
```

建議改為：

```text
audio chunk
  -> STT candidate generator
      - Groq Whisper
      - SenseVoice
      - optional local/faster model
      - previous/next chunk overlap
  -> evidence packet
      - audio_ref
      - stt_candidates[]
      - confidence
      - timestamps
      - profile/glossary
      - prior context
  -> transcript resolver
      - choose/merge best Korean source
      - mark uncertainty spans
  -> translation candidate generator
      - normal text translator
      - audio-grounded fallback for uncertain spans
  -> subtitle policy
      - emit draft quickly
      - optionally revise within a small window
  -> QA/event log
      - store evidence packet
      - store decision trail
```

## 新核心資料型別

目前 pipeline 傳的是「文字」。下一代要傳「證據」。

```python
@dataclass(frozen=True)
class STTCandidate:
    engine: str
    text: str
    confidence: float | None
    avg_logprob: float | None
    no_speech_prob: float | None
    start_ms: int | None
    end_ms: int | None
    source_utterance_ids: tuple[str, ...]


@dataclass(frozen=True)
class TextSpan:
    start: int
    end: int
    reason: str


@dataclass(frozen=True)
class TranslationEvidence:
    evidence_id: str
    audio_path: str | None
    audio_start_ms: int
    audio_end_ms: int
    stt_candidates: tuple[STTCandidate, ...]
    selected_source: str
    resolver_confidence: float
    uncertainty_spans: tuple[TextSpan, ...]
    profile_id: str
    context_before: tuple[tuple[str, str], ...]
```

translator 不應只收到：

```python
text: str
```

而應收到：

```python
evidence: TranslationEvidence
```

## Phase 1：Evidence Packet，不換模型

目標：先重構資料流，不追求立即品質提升。

要做：

- STT event 保留 audio reference。
- sentence event 保留 source utterance ids 與 evidence source ids。
- translation event 保留：
  - selected source
  - candidate sources
  - confidence
  - resolver reason
  - audio dump path
  - source engine list

產出：

- `modules/pipeline_events.py` 新增 `TranslationEvidence` / `STTCandidate`。
- `modules/stt.py` 不只輸出一個 text，而是輸出 candidate。
- `modules/sentence_splitter.py` 聚合 candidate evidence。
- runtime event JSONL 能回放每句字幕的來源證據。

驗收：

- 現有 666 passed / 4 skipped baseline 維持。
- 每個 translation event 都能追到 audio/STT evidence。
- labeling tool 能顯示候選 STT 與 selected source。

## Phase 2：STT N-best Resolver

新增：

```text
modules/transcript_resolver.py
```

輸入：

- Groq Whisper candidate
- SenseVoice candidate
- chunk overlap candidate
- previous context
- profile glossary

輸出：

- selected Korean source
- resolver confidence
- uncertainty spans
- resolver reason

第一版 resolver 可用 rule-based：

- 多個 STT 結果高度一致：直接選。
- 差異只在人名/遊戲詞：套 profile glossary。
- 一個 candidate 明顯低信心或低韓文比例：降權。
- 差異大但都有合理韓文：標 `needs_llm_judge=True`。
- source 低品質或衝突太大：標 `needs_audio_check=True`。

第二版加入 LLM judge：

```text
You are resolving Korean livestream STT candidates.
Choose the most likely Korean source.
Do not translate.
Return JSON with selected_text, confidence, uncertainty_spans, reason.
```

驗收指標：

- STT disagreement rate
- resolver confidence distribution
- selected source 被 human label 判為正確的比例
- STT 錯誤佔 label 問題比例是否下降

## Phase 3：Low-Confidence Audio Fallback

不要全量 audio-to-translation。只在低信心時啟用。

觸發條件：

- STT candidates 差異大。
- selected source 低韓文比例或疑似亂碼。
- translator 輸出 meta garbage。
- source/target quality gate bad。
- profile 重要詞未命中。

優先模式：

```text
audio -> Korean transcript verification -> normal translator
```

而不是一開始就：

```text
audio -> Traditional Chinese translation
```

理由：

- Korean transcript verification 比 direct translation 更容易 debug。
- 可以比較 audio model 修正了哪個 source span。
- 仍可復用現有 translator/cache/profile/corrections。

實作：

- 新增 `modules/audio_fallback.py`
- 支援 provider abstraction：
  - Gemini audio
  - GPT audio
  - Qwen audio/local audio model
- 輸出仍回到 `TranslationEvidence.selected_source`

驗收指標：

- audio fallback invocation rate
- audio fallback rescue success rate
- audio fallback latency p50/p95
- 每場直播 token/cost 上限

## Phase 4：Draft + Refine Subtitle

這是 UX 架構變更，不建議第一步做。

目前字幕是 append/replace 最新文字。下一代要支援同一字幕 ID 的狀態更新：

```text
subtitle_id=123 draft
subtitle_id=123 final
subtitle_id=123 correction
```

流程：

```text
fast resolver + text translator
  -> emit draft within 300-800ms

slow resolver/audio fallback
  -> emit final/correction within 1-2s
```

需要改：

- subtitle event schema
- subtitle UI replace semantics
- dedup policy
- stale subtitle policy
- labeling UI 顯示 draft/final diff

驗收：

- latency p50 不惡化。
- final quality 比 draft 明顯提升。
- correction 不造成 UI 閃爍或讀者困擾。

## Phase 5：QA Worker / Correction Loop

C2 不應作為主翻譯路徑，但可以作為回饋系統。

背景 QA worker 做：

- 找 source/target mismatch。
- 找 profile term mistranslation。
- 找同 source 多譯不一致。
- 產生 correction candidate。
- 丟到 labeling/review tool 給人確認。

不要讓 QA worker 直接即時修改 glossary。應該保留 human approval。

## 不建議優先做的事

### 1. 只擴大 context memory

更大 context 可能改善一致性，但也會：

- 增加 token cost。
- 增加舊錯誤污染。
- 增加 latency。

它不能解決 STT source 錯誤。

### 2. 全量 audio-to-translation

除非 latency/cost 已被證明可接受，否則不建議全量走 audio model。

更好的策略是：

```text
normal path for high-confidence cases
audio fallback for low-confidence cases
```

### 3. 繼續只靠 label translation

label translation 如果沒有同步標注 source 是否正確，會把 STT 錯誤誤歸因成翻譯錯誤。

label schema 應拆成：

- STT correct?
- source segmentation correct?
- translation correct?
- profile/glossary correct?
- output timing acceptable?

## 建議實作順序

### Milestone 1：Evidence Baseline

- 新增 evidence 型別。
- translation event 帶 candidate/source evidence。
- labeling UI 能顯示 evidence。
- 不換模型。

成功條件：

- 全量測試通過。
- 每句字幕可追溯到 STT/audio evidence。

### Milestone 2：Dual STT Candidate

- Groq + SenseVoice 同時或條件式產生 candidate。
- resolver rule-based 選 source。
- runtime event 記錄 resolver decision。

成功條件：

- 人工抽樣 50-100 句，STT source 正確率提高。
- latency p95 可接受。

### Milestone 3：Audio Fallback

- 低信心才 call audio model。
- audio model 先產 Korean transcript verification。
- fallback 結果回到 normal translator。

成功條件：

- 低信心片段 rescue rate 可量化。
- fallback invocation rate 不超出成本限制。

### Milestone 4：Draft/Final Subtitle

- subtitle event 支援 draft/final。
- UI 支援同 ID replace。
- labeling tool 顯示 draft/final diff。

成功條件：

- live latency 不惡化。
- final quality 提升可被 label 證明。

## 新指標

必須新增下列 metrics/runtime fields：

- `stt.candidate.count`
- `stt.candidate.disagreement_rate`
- `resolver.confidence`
- `resolver.reason`
- `resolver.needs_audio_check`
- `audio_fallback.invoked`
- `audio_fallback.success`
- `audio_fallback.latency_ms`
- `translation.source_quality`
- `translation.error_attribution`
- `subtitle.revision_count`
- `subtitle.draft_to_final_ms`

label schema 也要支援：

- `stt_error`
- `segmentation_error`
- `translation_error`
- `profile_term_error`
- `timing_error`
- `audio_fallback_helped`

## 最小可行 PR 拆法

1. `pipeline_events`: 新增 evidence schema，不改行為。
2. `runtime_events`: translation event 加 evidence fields。
3. `stt`: 輸出 `STTCandidate`。
4. `sentence_splitter`: 聚合 candidate evidence。
5. `transcript_resolver`: rule-based resolver。
6. `translator`: 接收 selected source + evidence metadata。
7. `labeling_review`: 顯示 evidence packet。
8. `audio_fallback`: 低信心 fallback provider abstraction。
9. `subtitle`: draft/final semantics。

## 最終判斷

要突破目前品質上限，主線不是再調 translator，而是讓 translator 不再只依賴單一 STT 文字。

下一代架構應該把「一句字幕」視為一個帶證據的決策結果：

```text
audio evidence + STT candidates + resolver decision + translation + QA attribution
```

最小且最值得先做的方向是：

```text
multi-STT candidates + transcript resolver + low-confidence audio fallback
```

這能把問題從「翻譯模型猜錯了」重新拆成可量測、可回放、可改進的幾個環節：STT、切句、source resolve、translation、subtitle timing。
