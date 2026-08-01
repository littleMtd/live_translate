# live_translate 架構重構建議 2026-06-13

作者：Codex

狀態：建議採納為重構方向，但實作前仍需要用既有標註資料與 audio dump 做 gate 驗證。

同步註記：`ARCHITECTURE_PROPOSALS_20260612.md` 的 2026-06-13 第二輪補遺已採納本文件為執行版計畫，但補上幾個必要執行約束。本文已同步修正：speaker policy 必須早於評測集；Phase 0 與 Phase 1 可並行；schema normalization 必須有 golden-file replay 測試；每個 Phase 都要有 kill criteria；latency 與 GPU 佔用必須是驗收項目。後續 `PHASE0_EVAL_INVENTORY_20260613.md` 已記錄 littleM 拍板並修正：`Speaker policy = host-primary`。

## 0. 結論

這次重構的目標不應該是讓 pipeline 看起來更乾淨，而是提高 live 翻譯品質上限。依照目前程式碼與標註結果，真正卡住品質上限的是 source correctness：STT 只輸出單一文字、speaker/source 混入沒有 runtime 決策、forced cut residual 仍會製造高風險 evidence、translator 收到資料時已經沒有足夠資訊可以判斷來源可靠度。

因此建議方向是：

```text
AudioChunk
  -> STT candidates
  -> transcript resolver
  -> TranslationEvidence
  -> existing translator
  -> low-confidence audio fallback when needed
  -> QA/runtime diagnostics
```

這不是從零重寫。現有程式碼已經有 `AudioChunk`、`TranscriptionEvent`、`SentenceEvent`、`source_utterance_ids`、`evidence_source_utterance_ids`、runtime JSONL、labeling sampler、`source_chunk_usages`。第一階段應該把這些已有 evidence 升級為翻譯決策資料，而不是建立一套平行新系統。

最重要的排序是：

0. Speaker policy 已定為 `host-primary`；評測集 ground truth 依此建立。
1. 建立 baseline eval set 與 gate。
2. 並行做低風險 evidence schema normalization。
3. 同一音訊產生多個 STT candidate，先離線重播，再 shadow logging。
4. 加 transcript resolver，先 rule-based，再小比例啟用。
5. 只對低信心 case 啟用 audio fallback，而且先做韓文 transcript verification，不做全量 audio-to-translation。
6. QA sidecar 只產生 correction candidate，不自動改 glossary/corrections。

第一階段不要同時重做 subtitle UI、不要做 draft/final、不要上 diarization、不要上 LoRA、不要重寫 translation engine fallback、不要自動修改 glossary。每個 PR 結束時系統都必須維持今晚可開台使用；所有新路徑必須能用 config flag 關閉。

## 1. 目前主要架構與資料流

目前主流程由 `main.py` 串起四個 queue：

```text
audio_queue -> text_queue -> sentence_queue -> subtitle_queue
```

實際模組邊界如下：

```text
audio_capture.py
  WASAPI loopback + VAD/fixed chunk
  emits AudioChunk

stt.py
  AudioChunk -> one TranscriptionEvent
  primary/fallback STT engine selection

sentence_splitter.py + sentence_buffer.py
  TranscriptionEvent -> SentenceEvent
  natural/silence/forced cuts
  carry-forward current/evidence attribution

translator.py + translation_engines.py
  SentenceEvent -> target string
  prompt/profile/corrections/memory/fallback engine chain

subtitle_display.py
  target string -> tkinter overlay
```

這個架構目前的強項：

- pipeline 很薄，latency 容易控。
- translator 層已經累積大量 domain prompt、profile glossary、source-aware corrections。
- runtime event 與 labeling 工具已經能追到不少錯誤來源。
- `SentenceBuffer` 已經把 carry-forward residual 的 prior chunks 放進 `evidence_source_utterance_ids`，避免把所有 prior chunks 都算成 current source。

這個架構目前的上限：

- STT 層在 live path 只輸出一個 transcript，沒有 candidate set。
- SenseVoice/Groq 是 primary/fallback 或 probe，不是同一 chunk 的候選比較。
- translator engine 介面只接受文字、system prompt、incomplete、history，沒有 source candidate、uncertainty span、speaker/source decision。
- subtitle 層只接收純字串，沒有 subtitle id、draft/final、replacement semantics。
- speaker/source attribution 主要存在標註資料，runtime 沒有明確策略。

## 2. 設計目標

重構目標應該是提高翻譯品質上限，而不是抽象化模組。

品質上限可以拆成：

```text
live quality ceiling =
  source correctness
  x segmentation correctness
  x translation correctness
  - latency/cost/debug penalties
```

目前 translator 已經非常努力地處理 STT 錯誤。`translation_prompts.py` 裡大量規則都在要求模型修正 STT raw string error、過濾垃圾、保留不確定內容；`translation_corrections.json` 也累積了 profile term 與 source-aware replacement。這代表下游優化已接近現有架構可做到的上限。

下一個品質突破點不是再塞更多 prompt，而是讓 translator 之前的 source selection 變得可比較、可回溯、可拒絕、可 fallback。

## 3. 已知問題與品質瓶頸

### 3.1 STT 單一路徑是最大瓶頸

`STTEngine.transcribe_event()` 目前對一個 `AudioChunk` 產生一個 `TranscriptionEvent`。當這個 transcript 錯了，後面所有模組只能在錯誤 source 上工作。

現有 fallback engine chain 主要處理 API failure、rate limit、空結果或壞結果，不能處理「Groq 和 SenseVoice 對同一段音訊有不同合理 transcript」這類問題。

必要改動是把 STT 從：

```text
AudioChunk -> TranscriptionEvent
```

改成：

```text
AudioChunk -> STTCandidate[] -> ResolvedTranscript
```

但第一階段應該相容輸出原本的 `TranscriptionEvent`，讓 downstream 不必一次全部改。

### 3.2 Speaker/source attribution 沒有進入 runtime 決策

標註系統已經有 `host_only`、`clip_or_other_speaker`、`host_over_clip`、`wrong_speaker_selected`、`audio_source_mismatch` 等 tags。這些 tags 說明錯誤不是只有 STT acoustic problem，也包括「到底該翻誰的話」。

目前 runtime pipeline 沒有 `speaker_policy`。這會導致兩個問題：

- translator 不知道目前應該 host-only、as-heard，還是 dominant speaker。
- labeling 能事後指出 speaker/source 錯，但 runtime 無法用這些資訊做 routing。

第一階段不建議上完整 diarization 或 source separation，但要先把 policy 與 metadata 納入 runtime event：

- `speaker_policy`
- `speaker_source_confidence`
- `source_mix_flags`
- `speaker_source_tags` when available from labeling/replay
- `audio_source_mismatch_risk`

### 3.3 Carry-forward 方向正確，但仍缺 span-level evidence

目前 `sentence_buffer.py` 已經把 forced cut residual 的 prior chunks 改放到 evidence，不再全部算成 current source。這是正確方向，不應該第一階段重做。

仍然缺的是 text/span-to-chunk offset。沒有 offset 時，系統只能知道某句話使用哪些 utterance 當 current/evidence，不能知道句中哪一段對應哪個 chunk。這會影響：

- forced residual 的精細 attribution。
- audio fallback 只重查不確定 span 的能力。
- `source_chunk_usages` 對 current/evidence 的解釋力。

建議：第一階段保留 current/evidence split；第二階段再評估 segment timestamp 或 token span mapping。

### 3.4 source_chunk_usages 已經有價值，但不應只靠後處理推斷

目前 `sample_labeling_cases.py` 會從 runtime translation event 的 source/evidence ids 建出 `source_chunks` 與 `source_chunk_usages`。這對 labeling 很有用，但它仍然是在事後重建 usage。

重構後 runtime event 應該直接保存更多 usage 決策資料：

- `candidate_id`
- `source_kind`: current/evidence
- `resolver_role`: selected/suppressed/supporting/conflicting
- `role_reason`
- `first_seen_translation_index`
- `prior_translation_indices`
- `audio_ref`
- `stt_status`
- `speaker_source_risk`

這樣 labeling 不只知道哪些 chunks 被使用，還能知道為什麼被使用或為什麼被拒絕。

### 3.5 runtime diagnostics 強，但還不能驅動 live routing

`utils/runtime_events.py` 已經有 schema version、translation quality flags、latency/cost 相關資訊。問題是這些診斷多半是「翻完之後才知道」。它們應該被提升為前置 routing signal：

- low source Hangul ratio
- low confidence STT
- high no-speech probability
- candidate disagreement
- profile term likely missed
- forced cut residual
- speaker/source mismatch risk
- translation meta leak or untranslated target

這些 signal 應該能觸發 resolver 降信心、audio fallback、或 QA sidecar，而不是只寫 log。

## 4. 對兩份提案的看法

### 4.1 Codex 提案

Codex 提案的主方向正確：multi-STT candidates、transcript resolver、low-confidence audio fallback，這些是最可能提高品質上限的部分。

需要修正的是 Phase 1 的描述。現有專案不是完全沒有 evidence pipeline，所以不應該重寫一套新的 `TranslationEvidence` 取代既有欄位。更好的做法是：

- 保留現有 `TranscriptionEvent` / `SentenceEvent`。
- 加入 candidate/resolver fields。
- 讓舊 downstream 繼續吃 selected text。
- 讓 runtime event 開始記錄 selected/suppressed candidates。

也就是 incremental evidence promotion，不是 big-bang evidence rewrite。

### 4.2 Claude 提案

Claude 提案補上的 gate 很重要。任何會提高 latency、API cost、debug cost 的模型路徑，都應該先用既有錯誤集驗證 rescue ceiling。

建議採納以下 gate：

- 30 到 50 個已知 STT error case 跑 SenseVoice/Groq candidate comparison。
- 20 個 audio dump 跑 audio transcript verification。
- 如果 dual-STT rescue rate 太低，或 audio verification 不能穩定修正韓文 transcript，就不要急著把它們接進 live path。

但不建議讓 gate 阻擋低風險 schema normalization。把 evidence 欄位補齊、把 runtime log 變得可 replay，成本低且後續必要，可以先做。

## 5. 必要重構

### 5.1 Evidence model normalization

必要。

新增或擴充以下資料概念：

```python
STTCandidate:
    candidate_id
    utterance_id
    engine
    model
    text
    status
    confidence
    avg_logprob
    no_speech_prob
    segments
    audio_ref
    overlap_seconds
    vad_cut_reason
    latency_ms
    prompt_budget

ResolvedTranscript:
    selected_text
    selected_candidate_id
    candidate_summaries
    resolver_confidence
    resolver_reason
    uncertainty_spans
    needs_audio_check
    source_utterance_ids
    evidence_source_utterance_ids

TranslationEvidence:
    sentence_text
    incomplete
    resolved_transcript
    speaker_policy
    source_mix_flags
    source_chunk_usages
```

注意：這些不需要一次全部改成新的 class。第一階段可以先在 runtime event 與 `SentenceEvent` metadata 中新增欄位，讓 downstream 維持相容。

預期品質影響：

- 直接翻譯品質：中。
- debug 成本：大幅降低。
- 後續 resolver/fallback 能力：必要前置。

### 5.2 Multi-STT candidate generation

必要，但必須先 shadow。

目標不是把 SenseVoice 當 fallback，而是同一個 `AudioChunk` 同時或條件式產生多個 candidate。初期可以：

- Groq 仍為 live selected path。
- SenseVoice 在 shadow mode 產生 candidate。
- runtime log 記錄 candidate disagreement。
- 不影響字幕輸出。

需要觀測：

- candidate disagreement rate
- SenseVoice rescue rate
- profile term hit/miss
- false rescue rate
- extra latency
- GPU/CPU cost

預期品質影響：

- 若 rescue rate 足夠，高。
- 若 candidate 只是製造更多噪音，應保持 shadow，不進 live path。

### 5.3 Transcript resolver

必要。

resolver 是這次重構最核心的模組。它應該放在 STT 與 splitter/translator 之間，或至少在 sentence metadata 形成前產生 resolved source。

v1 不應先做複雜 LLM agent。建議規則：

- 候選完全相同或高相似，直接通過。
- low Hangul ratio、template garbage、low avg_logprob、高 no_speech 降權。
- profile glossary 命中者加權。
- candidate 互相衝突時標 `resolver_confidence=low`。
- forced cut residual 或 evidence-only source 降信心。
- 只有高價值低信心 case 才交給 LLM judge。

輸出仍然是一段 selected Korean transcript，讓現有 translator 可以繼續工作。

預期品質影響：

- 高，因為它在翻譯前修正 source。
- latency 風險中等，可用 timeout 與 fallback-to-primary 控制。

### 5.4 Low-confidence audio fallback

必要，但不應全量啟用。

fallback 目標是 transcript verification，不是 direct audio-to-translation。

推薦流程：

```text
low-confidence evidence
  -> audio transcript verification
  -> corrected Korean source
  -> existing translator
```

觸發條件：

- resolver candidate disagreement
- low STT confidence
- source quality flags bad
- profile term likely missed
- speaker/source mismatch risk
- forced cut residual with low confidence
- translation output quality flags bad

限制條件：

- invocation rate cap
- per-request timeout
- p95 latency budget
- API cost budget
- fallback result 必須寫入 runtime event
- fallback 不得繞過 glossary/corrections/translator

預期品質影響：

- 對 hard STT case 高。
- 成本與 latency 風險高，所以必須 gated、限流、可關閉。

### 5.5 Speaker/source policy

必要，但第一階段只做 policy 與 metadata。

需要先明確決定 live subtitle 的產品規則：

- host-only：只翻 streamer 主聲道或主語者。
- as-heard：聽到什麼翻什麼。
- dominant speaker：翻當下最清楚或最大聲的語者。

如果沒有 policy，speaker attribution 就無法評分。標註資料中的 `wrong_speaker_selected` 也無法轉成 runtime 改進。

第一階段建議：

- config 增加 `speaker_policy`。
- runtime event 增加 `speaker_source_policy`。
- labeling/export 增加 policy-aware error attribution。
- 暫不做 diarization。

預期品質影響：

- 對 mixed audio 高。
- 短期直接提升有限，但會讓後續資料與模型決策變清楚。

## 6. 可延後重構

### 6.1 Draft/final subtitle

可延後。

這個功能可以改善 UX，讓快速 draft 先出、慢速 resolver/fallback 後補 correction。但目前 subtitle path 只傳純字串，沒有 subtitle id、replacement semantics、revision count、draft/final status。

若第一階段就做，會同時牽動：

- translator output event
- subtitle queue
- subtitle display stale policy
- duplicate suppression
- labeling diff
- user-visible correction UX

它不是 source correctness 的前置條件，所以應該等 resolver/fallback 穩定後再做。

### 6.2 Structured rolling memory

可延後。

目前已有 context window、translation memory、profile glossary、corrections。更複雜的 memory 對錯 STT、錯 speaker、錯 forced cut 幫助有限，還可能增加 prompt 污染與 latency。

只有在 source correctness 已改善後，memory 才有更高價值。

### 6.3 Span-level attribution

可延後到第二階段。

span-to-chunk mapping 對 forced cut、audio fallback、labeling 很有價值，但實作成本不低。第一階段可以保留 current/evidence split；第二階段再根據 segment timestamp 或 token alignment 做更細 evidence。

### 6.4 LoRA、VOD SRT、chat translation

可延後，且應視為獨立產品或資料資產方向。

LoRA 可能降低成本或改善風格一致性，但不能解決 wrong source。VOD SRT 與 chat translation 有價值，但不是 live pipeline 的核心品質瓶頸。

## 7. 不建議重構

### 7.1 不建議全量 audio-to-translation

原因：

- debug attribution 會變差。
- 會繞過既有 glossary、corrections、translation memory。
- 成本高。
- latency 高。
- 錯誤時很難知道是聽錯、翻錯、還是 speaker 選錯。

audio model 應該作為 low-confidence transcript verification fallback，而不是常態主路徑。

### 7.2 不建議 big-bang 重寫 pipeline events

現有 event schema 已有不少正確資產。大改會增加測試成本與 labeling 斷裂風險。應該採取相容擴充：

- 舊欄位保留。
- 新欄位 optional。
- downstream 逐步讀新欄位。
- runtime schema version 增加，但 sampler 保持 backward compatible。

### 7.3 不建議 QA worker 自動改 glossary

QA sidecar 應該產生 correction candidate、error attribution、profile term suggestion，但不能直接改 `translation_corrections.json` 或 `streamer_profiles.json`。

這些資產會直接影響 live path，必須 human approval。

### 7.4 不建議第一階段做 diarization/source separation

speaker/source attribution 是必要方向，但直接上 diarization 風險高。先把 policy、metadata、labeling feedback 建好，再用資料決定是否需要模型級 speaker separation。

## 8. 建議實作順序

### Phase -1: Speaker policy 決策

已完成。決議：`host-primary`。

任務：

- live subtitle speaker policy 採 `host-primary`：host 有聲時優先 host；host 沉默且 clip/其他語者是唯一清楚語音時,可翻該非 host 語音。
- 把這個 policy 寫進 config 與 labeling instructions。
- 依 policy 定義什麼叫 source 正確、speaker 錯誤、audio source mismatch。

成功條件：

- 標註者與評測腳本對「翻對誰」有同一個答案。
- 後續 eval set 不需要因 policy 改變而重標。

放棄或暫停條件：

- 若後續產品層改變 policy，正式 eval set 必須重標；在此之前不應混用 as-heard 或 dominant-speaker ground truth。

### Phase 0: Baseline 與 gate

不改 live 行為。

任務：

- 依 speaker policy 從現有 labeling logs/audio dumps 建 50 到 100 筆 regression/eval set。
- 覆蓋 STT error、forced cuts、speaker mix、profile term、translation error、latency outlier。
- 混入隨機抽樣與 hold-out，避免只對舊 pipeline 的失敗樣本過擬合。
- 先量現行 end-to-end p50/p95，定每階段 latency 紅線；建議 p95 不超過現值加 1 秒。
- 對 30 到 50 個已知 STT error case 跑 SenseVoice/Groq candidate comparison。
- 對 20 個 audio dump 跑 audio transcript verification。
- 定義 rescue rate、false rescue rate、latency、cost。

成功條件：

- 知道 dual-STT 是否值得進 shadow mode。
- 知道 audio fallback 是否真的能修 transcript。
- 建立後續每次改動的品質比較基準。

放棄或暫停條件：

- 如果 dual-STT rescue ceiling 低於 15%，先停止 resolver 主線，只保留 diagnostics。
- 如果 audio transcript verification 無法穩定修正韓文 source，audio fallback 不進 live path。
- 如果評測集標註一致性不足，先修 label schema，不繼續比較模型。

品質影響：

- live 無直接提升。
- 避免盲目重構，決策價值高。

### Phase 1: Evidence schema normalization

低風險，應先做。

任務：

- 在 `pipeline_events.py` 增加 candidate/resolver/evidence metadata。
- STT runtime event 補 `candidate_id`、`audio_ref`、candidate status。
- translation runtime event 補 `selected_source`、`resolver_confidence`、`candidate_summaries`、`source_chunk_usages`。
- labeling sampler 支援新欄位，但保留舊欄位 fallback。
- collection sanity report 檢查新欄位 join rate。
- 加 golden-file replay 測試：同一段既有 JSONL 重播後，event diff 只允許新增欄位，不允許舊欄位語義漂移。

成功條件：

- 每條 translation 可回溯到 candidates、selected source、current/evidence chunks。
- `source_chunk_usages` 可以區分 selected/supporting/conflicting/suppressed。
- sampler、labeling、analysis scripts 都能讀舊 log 與新 log。

放棄或暫停條件：

- 如果 schema change 需要同步重寫多個 consumer 才能維持舊功能，拆小；不得讓 live path 進入半遷移狀態。
- 如果 golden-file diff 出現舊欄位語義改變，停止合併。

品質影響：

- 直接中等。
- 後續高。

### Phase 2: Multi-STT shadow mode

任務：

- 同一 `AudioChunk` 產生 Groq 與 SenseVoice candidates。
- 第一輪只跑錄音重播，確認 GPU/CPU 餘裕後才掛 live shadow。
- live selected text 仍使用原本主路徑。
- runtime 記錄 disagreement、confidence、latency、term hit/miss。
- shadow report 納入 GPU 佔用、功耗、p50/p95 latency、API cost。
- 加 shadow evaluation report。

成功條件：

- 能量化哪些錯誤 case 可被第二 STT 救回。
- 不影響 live latency 或 subtitle。

放棄或暫停條件：

- 如果直播時本地 GPU 佔用污染 baseline 或造成掉幀，退回離線 shadow。
- 如果兩週 shadow 後 rescue ceiling 低於 15%，停止 multi-STT live 投資。

品質影響：

- live 低。
- 決策高。

### Phase 3: Resolver v1 shadow then active

任務：

- 新增 `transcript_resolver.py`。
- 先 rule-based。
- shadow mode 比較 resolver selected text 與現有 selected text。
- 小比例或 config flag 啟用。

成功條件：

- resolver 的 selected source 在 eval set 上明顯降低 STT error。
- rescue : false-correction 至少達到 10:1。錯誤救援比沒救到更毒，不能只看淨改善。
- latency 在 budget 內。

放棄或暫停條件：

- 如果 false correction 不能被清楚偵測或回滾，resolver 不進 live。
- 如果 p95 latency 超過紅線，resolver 只能保留 shadow 或低信心觸發。

品質影響：

- 高。

### Phase 4: Low-confidence audio fallback

任務：

- 新增 `audio_fallback.py` 或 equivalent adapter。
- 只做 Korean transcript verification/correction。
- 僅對 low-confidence evidence 觸發。
- 加 rate cap、timeout、cost cap。
- fallback 結果寫回 runtime event。

成功條件：

- hard STT case rescue rate 明顯。
- 平均 latency 不被拖垮。
- 可用 runtime event 追到每次 fallback 是否值得。

放棄或暫停條件：

- 如果 fallback 經常產生流暢但錯的 Korean transcript，停止 live 啟用。
- 如果 cost 或 p95 latency 超過預算，只保留離線 QA/labeling 用途。

品質影響：

- 高，但風險高。

### Phase 5: QA sidecar

任務：

- translation 後台 QA logging。
- 產生 error attribution：STT、segmentation、speaker/source、translation、profile term。
- 產生 correction candidates。
- 不自動套用。

成功條件：

- 標註與 correction review 成本下降。
- 可累積高品質 profile/correction 資產。

品質影響：

- 短期中等。
- 長期高。

### Phase 6: Draft/final subtitle

只有在 resolver/audio fallback 證明有效，但 latency 對 UX 有明顯傷害時才做。

任務：

- subtitle id
- draft/final/correction status
- replace semantics
- revision count
- stale policy
- labeling diff

品質影響：

- 對 source correctness 低。
- 對 UX 與慢路徑可用性中到高。

## 9. 第一階段不要一起動的範圍

第一階段只處理 host-primary policy 落地、baseline、evidence normalization、shadow logging。Phase 0 與 Phase 1 可並行；所有新路徑必須掛 config flag，且每個 PR 結束時 live path 仍可直接開台使用。不要同時做：

- subtitle draft/final UI
- diarization
- source separation
- LoRA
- VOD SRT
- chat translation
- translation engine chain 重寫
- DB/cache 重寫
- 自動 glossary mutation
- prompt 大改
- queue 策略大改

理由很簡單：這些不是目前品質上限的主瓶頸，或風險太高，會讓 source correctness 的實驗結果變得不可解釋。

## 10. 每個改動對品質上限的預期影響

| 改動 | 品質上限影響 | latency/API cost | debug 成本 | 建議 |
|---|---:|---:|---:|---|
| Speaker policy decision | 高，定義什麼叫翻對 | 低 | 大幅降低 | 已完成：host-primary |
| Baseline eval set/gate | 間接高 | 低 | 降低 | 必做 |
| Evidence schema normalization | 中，且是前置 | 低 | 大幅降低 | 必做 |
| Multi-STT candidates | 高，取決於 rescue rate | 中 | 中 | shadow 後啟用 |
| Transcript resolver | 高 | 中 | 降低 | 必做 |
| Low-confidence audio fallback | 高 | 高 | 中 | 限流啟用 |
| Speaker/source policy metadata | 高，尤其 mixed audio | 低 | 降低 | 必做 |
| Span-level attribution | 中 | 中 | 降低 | 第二階段 |
| QA sidecar | 中到高，偏長期 | 中 | 降低 | 提早做 logging |
| Draft/final subtitle | source correctness 低，UX 中高 | 中 | 增加 | 延後 |
| Structured rolling memory | 低到中 | 中 | 增加 | 延後 |
| LoRA | 中，偏成本/風格 | 中到高 | 增加 | 延後 |
| 全量 audio-to-translation | 不確定 | 高 | 大幅增加 | 不建議 |

## 11. 最終建議

live_translate 最有價值、最低風險、最能提高翻譯品質上限的重構，不是把 translator 包成更漂亮的 abstraction，而是把 source 決策提前做對。

第一階段應該只做四件事：

1. 依已定的 `host-primary` speaker policy 建立評測 ground truth，避免混入 strict host-only 或 dominant-speaker 標準。
2. 用既有標註與 audio dump 建可重跑的 quality gate，並量現行 latency baseline。
3. 把現有 evidence 欄位升級成完整可追蹤 schema，並用 golden-file replay 防止欄位語義漂移。
4. 用離線重播與 shadow mode 量測 dual-STT 與 audio verification 是否真的能救錯。

第二階段再讓 resolver 進 live path。第三階段才加入低信心 audio fallback。等 source correctness 有實際提升後，再考慮 draft/final subtitle、structured memory、LoRA 或其他產品功能。

這樣切分的好處是每一步都能回答同一個問題：它是否真的讓錯誤 source 變少，或讓錯誤 source 更容易被偵測與修正。每個 Phase 開工前都要寫成功條件、kill criteria、latency/cost 紅線；只有能回答這些問題的重構，才應該進第一批。
